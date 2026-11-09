# Custom dialogs on the CR100

For the three bottom buttons, see [SOFTKEYS.md](SOFTKEYS.md). The November 3
new-instance tests distinguish metadata-selected stock actions, the service-name
caption of the Now Playing library key, and labels inside service menus.

Current cross-project status and remaining work are indexed in
[RESEARCH.md](RESEARCH.md). The adopted settings route is a separate stock Music
service using explicit nonplayable-item library actions; it is not implemented
or deployed at this documentation checkpoint. Its [4.3 runtime proof and 4.3.2
static comparison](CONTROLLER-DEPLOYMENT.md#stock-firmware-menu-alternative) are
separate from the patched native-settings UI.

Research reviewed November 1, 2026. Claude completed an emulator proof of concept;
this follow-up checked its screenshots, Flash actions, native disassembly, and the
corresponding 4.3.2 firmware. No production hook or physical-controller test was
performed. Evidence is in [runs/dialog-research-2026-11-01](runs/dialog-research-2026-11-01/).

**Emulator follow-up, November 2:** custom search input, browse choices, ratings
with a custom result popup, library add/remove, playlist save/rename/delete, and
alarm submission have now been exercised in the original 4.3 runtime. See the
[runtime results and fixtures](runs/interactive-emulator-2026-11-01/README.md).
The tested library-removal confirmation retained fixed firmware wording.

## Result

The bridge can display arbitrary plain text in the stock CR100 message box by
sending an **AVTransport `LastChange` event** from the selected virtual player.
This is a player transport-error notification, not a custom SMAPI response type.
A service hosted separately from the player needs a bridge-side callback to use it.

![Custom text over the playlist browser](runs/dialog-research-2026-11-01/click-dialog.png)

Claude sent the event manually after selecting Spotify > My Playlists. The
screenshot proves custom text renders over the browser; it does **not** demonstrate
an automatic `getMetadata`-to-dialog hook. Earlier attempts to replace another
session's test peer were rejected; the successful tests used one-off NOTIFYs
without replacing it. The final test cleared the transport status to `OK`.

## Wire format

Use the CR100's current AVTransport event subscription: its callback URL, SID, and
next sequence number. Do not hard-code the emulator's address, port, or SID.
Send from the virtual player the controller is monitoring for the selected room
(normally its group coordinator).

The **inner** LastChange XML is:

```xml
<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">
  <InstanceID val="0">
    <TransportStatus val="ERROR_PROBE"/>
    <TransportErrorDescription val="Spotify&#10;&#10;You clicked an entry - this dialog was sent by the bridge."/>
  </InstanceID>
</Event>
```

`ERROR_PROBE` is the value used in the successful tests. The UI accepts statuses
other than `OK` and `NOT_IMPLEMENTED`; the name itself supplies no display text.
The example's Spotify heading is part of the message body, not a separate title.

XML-escape that whole document as the text of `LastChange` in a GENA propertyset:

```xml
<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">
  <e:property>
    <LastChange>&lt;Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/"&gt;&lt;InstanceID val="0"&gt;&lt;TransportStatus val="ERROR_PROBE"/&gt;&lt;TransportErrorDescription val="Spotify&amp;#10;&amp;#10;Hello from the bridge."/&gt;&lt;/InstanceID&gt;&lt;/Event&gt;</LastChange>
  </e:property>
</e:propertyset>
```

Send it in an HTTP `NOTIFY` to the subscribed callback, with:

```http
Content-Type: text/xml; charset="utf-8"
NT: upnp:event
NTS: upnp:propchange
SID: <current subscription SID>
SEQ: <next sequence number for this subscription>
```

Attribute values must be escaped before escaping the outer document. `&#10;`
inside the inner attribute preserves a newline; a literal newline in an XML
attribute becomes a space. Python ElementTree handles this when serializing an
attribute value containing `\n`, then serializing that inner XML as outer text.

After delivery, publish a further AVTransport event with `TransportStatus="OK"`
and an empty `TransportErrorDescription`. Keep the send and reset ordered; a
queued sender that reads state later can otherwise send only the reset. Clearing
the status is not a command to dismiss the already-visible message box.

## Firmware path and verification

The browser checks whether the incoming update contains a non-OK
`TransportStatus`, then calls:

```text
g_TransportErrorHandler.adviseError(status, "", description)
  -> RinconSystem.formatTransportError(description)
  -> MsgBoxDlg / pushLoadAndCenterPopup
```

`TransportErrorHandler_adviseError` ignores `OK` and `NOT_IMPLEMENTED`. It sets
`dismissOnKeyPress=true`, `timeoutMs=10000`, and `minMs=1000`. If its popup already
exists, it changes that popup's text instead of creating another.

The native formatter parses an initial decimal integer with `strtol`. If no
integer is parsed, or the following character is not a comma, it copies the text
unchanged into a 2048-byte buffer, terminating at byte 2047. Avoid messages
starting with a numeric CSV prefix, including signed or whitespace-prefixed
numbers followed by a comma; those enter the error-code formatting branch.
The buffer limit is not a guarantee that 2047 bytes fit legibly on the screen.

| Evidence | 4.3 | 4.3.2 |
|---|---|---|
| Native formatter address | `0x46d640` | `0x46d840` |
| Flash adviseError offset | `0xc205f` | `0xc216f` |
| Flash browser handler offset | `0xaaa7e` | `0xaab89` |
| Flash Now Playing common handler offset | `0x40b66` | `0x40b5b` |

The four checked Flash functions (above, plus message-box key handling) have
identical decoded operations after normalizing branch targets relative to their
function offsets. The native formatter's instruction bytes through its literal
pool boundary match exactly, as do its calling wrapper's instructions. The
formatter's imported `strtol`, `strncpy`, and `strlen` targets are unchanged.
Hashes, comparison results, and decoded actions are preserved beside the images.

The 4.3.2 inputs were extracted from the existing signed update package
`../update-analysis/7.1-access/cr100/downloads/25.2-50130-1-2.upd`.
Its cramfs starts at `0x9bf33` (record header at `0x9bf23`).
To re-decode an extracted, uncompressed MainUI.swf:

```sh
python3 runs/dialog-research-2026-11-01/swf_actions.py /path/to/MainUI.swf TransportErrorHandler_adviseError BrowsePanel_updateUIWithAVTData NowPlayingPanel_updateCommonUIWithAVTData MsgBoxPanel_ButtonHandler
```

## Observed behavior and limits

- Stock 4.3 in the emulator displayed arbitrary messages over browse results;
  long messages wrapped and explicit line breaks rendered correctly.
- The message auto-dismissed. An OK press after the minimum interval dismissed
  it without opening the highlighted entry in the recorded test.
- A later event omitting `TransportStatus` did not reopen the message. Explicitly
  resetting to `OK` remains preferable to retaining a synthetic error in state.
- After track > Play Now, the test moved to Now Playing and showed no dialog,
  even after a fresh-SID send. Now Playing has a matching error handler, but its
  panel/source update path differs. Claude attributed this to the source-less
  test player; that explanation is an inference, not a proven hardware result.
- Selecting Add to End of Queue keeps the browser visible, so its handler is a
  promising integration point. Automatic send-before-play-response ordering and
  physical 4.3.2 behavior remain untested.
- This provides a notification message box. It does not expose custom buttons,
  text input, a confirmation result, or a dismissal callback to the bridge.

## Where to integrate in this bridge

`tools/zoneplayers.py:HouseholdState.last_change()` currently emits
`TransportStatus="OK"` for backend rooms; the base
`tools/zp80/zp80_peer.py:PeerState.last_change()` does the same for simulated rooms.
Both paths would need a temporary per-player status/description override.
`VirtualPlayer.publish_changed()` suppresses duplicate propertysets, and the base
`ZP80Peer.notify()` owns subscription locking, sequence numbers, and HTTP delivery.
Reuse that machinery instead of reading SIDs from packet captures as the test did.

A special enumerable service entry can be handled in
`tools/spotify_service.py:get_metadata()` or `tools/smapi_stub.py:get_metadata()`:
serve its normal response and request a message on the appropriate virtual player.
That connection is not implemented. SMAPI browse requests identify the controller
but do not directly supply the selected virtual room; the bridge needs to resolve
that context. Do not broadcast a message to every room as a substitute.

Also avoid interpreting every `getMetadata` as a click: the controller pages and
caches results. Use an explicit item ID and account for retries/caching. A playback
entry instead reaches `HouseholdState.music_service_action()` with the target
player already known, but Play Now introduces the panel-transition limitation
above. Preserve the error event until it has actually been serialized and sent,
then clear it through the same ordered notification path.

Other routes Claude found were less useful: browse errors/empty results render
in the list area; queue/play SOAP errors show firmware-selected text; partner
status has a separate native-to-Flash service-error path but was not tested and
is not naturally tied to selecting a service entry.

## Further stock interaction routes

A follow-up static audit found more useful UI flows. Evidence and decoded actions
are under [runs/interactive-research-2026-11-01](runs/interactive-research-2026-11-01/).
The analysis below records the initial leads. The subsequent emulator run
confirmed several of them; its runtime results linked above take precedence
over the initial uncertainty. No physical-controller test or production bridge
implementation is claimed.

### Text input through service search

This is the clearest generic text-entry channel. The existing stub advertises
search with `Capabilities="1"`. Its `getMetadata(id="search")` response supplies
search categories with their own IDs and titles (`itemType="search"`). The
controller opens its stock keyboard for a category, then sends the submitted text
back through SMAPI `search(id=<category>, term=<entered text>, ...)`.

`SearchNode_onOK` (SWF 4.3 `0xb322e`, 4.3.2 `0xb333e`) creates the keyboard with a
100-character maximum. Its heading incorporates the category title; the Search
and Cancel controls stay firmware-defined. This suggests categories such as
"Enter code" or "Find device", with the bridge interpreting the search term as
input and returning a list of results or next steps. Identical inputs, paging,
caching, and retries need care before using this channel for actions.

### Choices and result messages through service ratings

The native presentation-map parser supports `NowPlayingRatings`, a `Ratings`
collection, and rating attributes `Id`, `StringId`, `AutoSkip`, and
`OnSuccessStringId`. `sonosApiRatings` obtains labels/data through
`trackRatingsModel.getRatingsMenuData()`, displays a choice menu, and invokes
`rateItem` for the selected value. This is a candidate for a few bridge-defined
commands with labels from the service's localized string table.

There is a second notification path here: `onRateItemCompleted` (4.3 `0x48a87`,
4.3.2 `0x48a98`) opens a message box for a nonempty result message with a **4-second
timeout**. A `rateItemResult` can contain `shouldSkip` and `messageStringId`;
the native code resolves the latter through the presentation strings. It does
not accept arbitrary literal message text in that field. The Flash callback's
`bShowAsPopup` parameter is unused in the decoded body: a nonempty message opens
the popup.

The service descriptor's `Presentation` section can point to `Strings` and
`PresentationMap` documents using their `Uri` and `Version` attributes. Candidate
XML fragments, native disassembly, and 4.3.2 parser comparisons are saved in
[native/NOTES.md](runs/interactive-research-2026-11-01/native/NOTES.md). The
fragments were derived statically and still need a real fixture test.
The later emulator run corrected the required nesting to include outer
`Presentation` and inner `Match` elements; use its tested XML rather than the
earlier candidate fragments.

This is not available on every song. The stock UI requires the matching
service-radio source with `directRadioType == DirectRadioSonosAPI` and a ratings
model with `numRatings > 0`. (The earlier audit's `currentSourceType == "OTHER"`
condition was an incorrect interpretation of an inverted Flash branch.)
An ordinary queue track alone does not expose this menu. The emulator test
confirmed it using a synthetic `x-sonosapi-radio:` source and a downloaded map;
both rating values and the custom acknowledgment were received/displayed.

### Confirmation through library/favorites removal

`BrowsePanel_showPlayMenu` offers a removal action when the content provider's
`canPerformActionOnObject(1, parentId, objectId)` permits it. The generic
`deleteLibraryItem` handler (4.3 `0xae713`, 4.3.2 `0xae81e`) asks the provider for
its prompt using `getStringForLibraryOp(objectId, 2)`, displays **No / Yes**, and
starts `createPerformActionOp(1, objectId)` only on Yes. No cancels locally rather
than reporting a negative response to the service.

The native presentation parser also supports `MenuItemOverrides` for
`AddTrackToFavorites`, `RemoveTrackFromFavorites`, `AddAlbumToFavorites`, and
`RemoveAlbumFromFavorites`. Its fields include `StringId`, `PromptStringId`,
`SuccessStringId`, `FailureStringId`, and `InProgressStringId`. These parsed fields
did not customize the tested confirmation flow. The runtime test found
capability 3 and a containing collection with ID `favorites` exposed removal.
Yes sent `deleteItem` with a `favorite` argument; No sent none. The tested
override did **not** replace the fixed prompt or menu label.

### Fixed player screens that already return structured input

- **Save Queue / Rename Playlist:** stock name entry returns a title through
  `AVTransport.SaveQueue` or a ContentDirectory title-change action. Existing-name
  checks can also open an overwrite confirmation. The bridge would need to
  implement persistence and update notifications; its current default replies do
  not provide that behavior.
- **Delete Playlist:** stock Yes/No prompt includes the supplied playlist title;
  Yes invokes ContentDirectory deletion. Captions and verbs remain fixed.
- **Alarms / sleep timer:** stock time, duration, weekday, room, and enable controls
  feed `CreateAlarm`, `UpdateAlarm`, and `ConfigureSleepTimer`. They could drive
  schedules in a bridge backend without inventing a form UI. This would be a new
  backend feature; no arbitrary alarm-form relabeling or unprompted opening of the
  editor was established.
- **Browse lists:** service collection IDs and labels can represent a small
  multi-step menu, returning further choices per selection. These are ordinary
  browse screens, not modal dialogs. Treat browse requests as potentially cached,
  paged, or repeated rather than as exactly-once button callbacks.

The checked playlist and alarm Flash handlers match between 4.3 and 4.3.2 after
normalizing branch targets relative to function offsets; see `player/comparison.json`.

### Leads that are not yet demonstrated

The native client implements `getExtendedMetadata`, `getExtendedMetadataText`,
and parses `relatedBrowse`/`relatedText`. That alone does not establish a stock
CR100 information/text screen: this audit found no corresponding Flash route.
Likewise, account/device-link setup and the local generic keyboard/menu constructors
are not evidence of a remotely callable arbitrary-form API. The generic popup
constructors' choices and callback methods are assembled by local ActionScript.

The runtime tests establish search input, browse choices, and service-radio
ratings as the most useful custom interaction surfaces. Library removal provides
a fixed-wording confirmation. Production wiring and retry/cache handling remain
separate implementation work. Keep the known AVTransport error route for
unsolicited notifications.
