# TuneIn Radio feature and implementation plan

Status: implemented in the development build. Date: November 3, 2026.
See [RADIO.md](../features/RADIO.md) for operation and [RADIO-ACCEPTANCE.md](RADIO-ACCEPTANCE.md)
for validation evidence and hardware/deployment boundaries.

Restore the CR100's original Radio experience using the public RadioTime/TuneIn API. Keep **Music > Radio** as the entry point, preserve the manual's workflows, and add current album artwork, richer metadata, practical discovery tools, and Radio settings. Radio must work without a TuneIn account or another Music-menu service.

The minimum release includes every Radio-specific workflow in the supplied 2008 manual, its shared playback and alarm integrations, and the user's artwork and settings requirements. Podcasts are visible by default. Alarms use a global provider-independent service so sources such as Spotify can be added later. Implementation phases below order the work; they do not make later minimum requirements optional.

## Navigation and browsing

Keep the original Radio root in this order:

1. Favorites
2. Search
3. Local Radio (saved city or area)
4. Music
5. Talk
6. Sports
7. Location
8. Podcasts
9. Radio Settings

Keep native Back, scroll-wheel, OK, context menus, Add to Favorites, Change Location, View Queue, room selection, and Now Playing behavior. Additions belong inside this Radio hierarchy. Internal native service discovery must not expose TuneIn as a second music service.

Favorites contains separate **Radio Stations** and **Radio Shows** lists. Add **Recently Played** there as an extension. Podcasts is a first-class, visible root entry with categories, show pages, episodes, favorites, and supported resume. A **By Language** root shortcut remains an optional Browse Options setting; enabling it appends it before Radio Settings without moving the original seven entries or hiding Podcasts.

Within Music, Talk, and Sports:

- Discover current categories from the API instead of hardcoding its changing category IDs.
- Present genre/topic folders alphabetically by default. Keep ordered station and editorial collections in their supplied order.
- Within a genre or topic, offer **Most Popular**, subgenres or related categories, **By Location**, and **By Name** where the provider supports them. Most Popular is the default station list.
- Use provider popularity ordering. Do not turn reliability, bitrate, or favorite counts into an invented popularity score.
- Keep all station languages available by default. A preferred language influences results only when configured.
- Display station name and useful callsign/frequency/location context. Use native rollover artwork and On Now/Host fields where applicable.
- Allow empty categories, unavailable items, missing schedules, and partial metadata without breaking navigation.

Fetch a branch only when opened. Follow the API's supplied continuation URLs, normally about 25 stations per page, and represent **More Stations**, **More Shows**, and episode continuation as ordinary folder choices. Deduplicate stable provider IDs. Freeze fetched page order and selected position for the browsing session; returning with Back restores the same view. Refresh on a new session or explicit refresh, with invalidation after relevant settings changes.

Do not crawl the catalog on entry or manufacture a full catalog count. Each exposed page has a truthful finite child count, including its continuation choice. Alphabetical browsing uses letter folders rather than requiring the controller to sort thousands of unloaded stations.

## Search

Restore the original **Stations**, **Shows**, and **Hosts** search choices, using the CR100 keyboard and Search soft key. Accept full and partial text, handle international text, preserve the query when returning from results, and provide clear no-results and retry behavior.

- Stations returns correctly classified station entries, with a direct Play action and Add to Favorites.
- Shows returns programs and podcasts, opening their live and archived listening choices.
- Hosts searches by host name and returns relevant associated programs using the public search/profile information. A reliable separate host-object API is not established; a host-associated-program adapter must pass named-host fixtures and live examples before this feature is complete. Arbitrary station results must not be relabeled as host matches.
- Add recent searches and Clear Recent Searches. Keep recent queries household-wide, with history disabled when the Radio History setting is Off.

Public search returns a bounded set of matches and does not reliably paginate; tested offset/page parameters were ignored. Do not invent search continuation or an exhaustive match count. Offer query refinement and the browsable directory for deeper discovery.

## Local Radio and location

Preserve **Local Radio (location)** and its **Change Location** soft key. Support both original selection methods:

- **Enter ZIP Code:** validate a five-digit U.S. ZIP, resolve it to coordinates, and call the API's supported `latlon` parameter. Direct ZIP query parameters are silently ignored by the tested public endpoint.
- **Pick a City:** use the provider's continent/country/state-or-region/city tree, retaining the provider region identity and display label.

Save the chosen place, coordinates or region ID, and display label. An explicit saved location takes precedence over IP detection and survives restarts. Use IP detection only as initial convenience when no location is saved. Changing location updates the native root label and invalidates affected lists on all controllers.

Keep the API's FM, AM, and Internet Only sections. Sort broadcast sections by frequency; retain provider order for Internet Only entries. The separate Location tree remains available for exploring other places without changing the saved home location.

## Favorites and custom stations

Store Radio favorites locally at household level, shared across controllers and playback backends. Save provider ID, content type, display snapshot, and ordering; resolve current stream URLs when needed. Adding a favorite creates a reference and leaves the directory item in place.

Required behavior:

- Native Add to Favorites for stations and shows, with duplicate detection and success/error feedback.
- Separate saved-station and saved-show browsing, including the native alarm source picker.
- Favorite removal through bridge management, matching the manual's desktop workflow; add native Remove from Favorites where the stock Radio action contract permits it.
- Rename a favorite's local display label and reorder favorites without modifying TuneIn's directory metadata.
- Preserve favorites across restart, configuration edits, and cache clearing. Keep unavailable favorites visible with an understandable state.

The bridge web UI takes the role of the manual's Desktop Controller for custom stations. Provide **Add Station**, **Edit Station**, **Test URL**, and **Delete**, with station name and stream URL required. Allow an optional logo and descriptive fields. Newly added stations appear in Radio Stations favorites with stable local IDs. Editing a stream URL applies to manually added stations; directory entries retain their provider identity. Deleting a custom station also handles its favorite reference consistently after confirmation.

Support direct MP3 streams as the manual minimum for custom stations. The manual also lists WMA for general Internet Radio: support unprotected WMA through a capable renderer or a verified bridge conversion route when required. Add compatible AAC/HLS and playlist URL handling according to the target renderer's capabilities. A URL test validates resolution and format without starting playback in a room. Custom stations do not require TuneIn submission or an account.

## Playback and rooms

Live Radio plays in the selected room or current group. Reuse existing grouping, coordinator selection, volume, per-room/group mute, and mute-all controls. Browsing and changing Radio preferences must not interrupt another room's playback.

Use a dedicated live-broadcast playback route. Starting Radio preserves the existing music queue and View Queue access. It must not pass a resolved HTTP stream through a helper that treats it as a new queue track or clears the queue. Queue-track playback subsequently switches out of Radio and clears its metadata overlay.

Resolve a station afresh when Play is invoked. Select a stream compatible with the actual speaker/backend, retain ordered alternatives, and attempt bounded automatic recovery on confirmed failures. Report unavailable, restricted, unsupported, or failed streams honestly. Do not count a provider placeholder recording as successful station playback.

The live Play/Pause key stops and restarts the live stream when true pause is unavailable. Restarting returns to live audio. Expose seek, progress, skip, and resume only for content and backends that support them. On-demand episodes use finite-track and queue semantics, honor the configured native OK-button behavior, and can support pause, seek, duration, and resume bookmarks when the backend permits them.

Direct Sonos and Home Assistant are both release targets. Radio identity, metadata, and artwork must follow the current room/group coordinator and reconcile with playback started by another controller or application. Saved favorites and history use canonical content IDs, independent of the backend.

## Album artwork and metadata

**Album artwork displayed together with metadata is a release requirement.** Verify the actual CR100 screen, including its regular Now Playing view and OK-to-enlarge artwork view. An image URL in XML is not sufficient evidence.

Maintain separate canonical fields for station identity, callsign/frequency, location, genre/language, current program and host, song title/artist/album, episode identity, stream status, and artwork identity. Adapt those fields to native Station, On Now, Host, track, artist, album, and image fields without confusing a program title with an album or a slogan with a song.

Artwork selection:

1. Use matching current song/album artwork supplied by the playback backend or TuneIn, including browse `playing_image` where appropriate.
2. If a song is identified but its cover is absent, perform a cached, conservative artist/title/album lookup through MusicBrainz and the Cover Art Archive. Prefer an exact release/recording identity when available; reject ambiguous matches.
3. For an on-demand episode or spoken program, use its episode/show artwork.
4. When a song cannot be identified or no trustworthy album cover exists, use the station logo, then a neutral Radio image. Keep metadata visible and do not represent a station logo as an identified album cover.

Unknown songs and absent cover art are genuine source-data limits. The required implementation includes album-cover retrieval and display; it cannot identify every broadcast recording from missing metadata. Missing art must have the explicit fallback above rather than silently retaining the previous song's image.

Use player/stream metadata events as the primary live update source. Supplement with TuneIn's cached directory, Describe, and Now Playing data. Respect its cache headers and TTLs; do not poll the public Now Playing endpoint every few seconds. When the API's cached song does not match the current stream identity, do not apply that song's cover to the new track.

On track or station change, immediately clear old song fields and artwork, publish the new identity with the correct fallback image, and apply arriving album art only if it still matches the active playback generation. Ignore late responses from previous tracks, stations, rooms, or episodes. Keep group members and multiple controllers consistent.

Proxy artwork through a controller-reachable HTTP endpoint, using the existing image infrastructure: baseline JPEG, native size/aspect limits, bounded image caches, concurrent-fetch deduplication, and revision-aware URLs. Artwork download or lookup must not block audio, room control, or scrolling. Cache successful cover matches and misses; enforce provider request limits centrally across rooms.

## Shows, podcasts, and schedules

Restore live programs, archived shows, and podcasts from the manual. Extend them with explicit episode browsing where the public API supplies episodes.

- A show opens its listening options, including stations carrying it live and available archives.
- Episodes display title, date, duration, and artwork where supplied, with pagination and correctly typed playback actions.
- Show favorites remain show references rather than frozen references to a particular episode.
- Add Continue Listening and per-episode bookmarks for verified seek-capable playback. Avoid promising resume for a live broadcast.
- Offer station schedules where supplied, linking programs to their listening options. Store UTC times internally and display the configured local timezone, including daylight-saving changes.
- Preserve native queue actions for finite episodes; live station playback remains separate from the saved music queue.

## Global alarms and sleep timer

Radio alarms and sleep timer are part of the minimum release. Implement alarms as a shared bridge subsystem, independent of Radio and the playback backend. Radio is the first content provider; adding a future Spotify provider must not require another scheduler, alarm database, or controller alarm UI.

Restore native Clock and Alarms integration: create, edit, enable/disable, and delete alarms; choose content, room, time, recurrence, volume, and duration; honor Include Linked Zones at firing time. Preserve Show Clock, Stop Alarm, and the documented nine-minute Snooze behavior. Radio stations are available in this release. The global web source picker is built from registered providers; Spotify becomes selectable there when its provider adapter is implemented and configured.

The stock CR100 alarm source picker contains hardcoded service choices. A provider registry alone cannot add Spotify to that screen. The native alarm adapter must prove a generic provider collection route or implement a narrow source-picker UI extension; that UI work must remain separate from the shared scheduler and store. Once that route exists, provider additions reuse it. Native and web edits always address the same global alarm records.

The shared architecture consists of:

- **AlarmService:** one authoritative create/read/update/delete, enable/disable, snooze, cancel, and status API used by native AlarmClock operations and the bridge web UI.
- **AlarmStore:** a versioned global store of alarm ID and revision, enabled state, local wall time and IANA timezone, recurrence, room, linked-room policy, volume, duration, provider ID, canonical source reference, optional account reference, and execution state. Keep this separate from Radio preferences/favorites and provider credentials. Resolve an account reference through the provider's existing credential store at execution time.
- **AlarmProvider registry:** each content provider exposes its available alarm sources, validates and describes a source, reports required capabilities, and resolves a fresh playback request at firing time. Persist logical references such as `radio:station:s34682`, `radio:custom:<id>`, and, for a future adapter, `spotify:playlist:<id>`. Do not persist temporary stream URLs or copy access tokens into alarm records.
- **AlarmScheduler:** one service owns recurrence, timezone/DST behavior, firing, nine-minute snooze, cancellation, duration expiry, restart recovery, missed-alarm policy, retries, and fallback. Providers do not implement their own clocks or timers.
- **AlarmExecutor adapters:** route a prepared request to the selected room/group through Sonos, Home Assistant, or another registered playback backend. A playback request distinguishes a live broadcast that preserves the queue from finite tracks/playlists with their own queue semantics. Use one execution owner per scheduled occurrence: the bridge scheduler or a verified native speaker delegate. Reconcile and confirm disabling the old owner before enabling a replacement; leave externally created native alarms externally owned unless explicitly adopted.
- **Shared alarm sessions:** journal each firing durably by alarm ID and scheduled UTC occurrence, with a playback generation and execution status. Prevent duplicate dispatch after restart or executor changes, and reconcile uncertain speaker acknowledgments instead of assuming exactly-once audio playback. Stop, snooze, and duration expiry must affect only playback still owned by that alarm; a later manual playback selection must not be stopped by an old timer.

Native speaker execution requires a verified stable source URI or a bridge resolver reachable from the real speaker's LAN, resolving current playback at firing time. A controller-only TAP address or a previously resolved temporary audio URL is insufficient. Use the bridge scheduler when that native execution contract cannot be established. Keep Show Clock preferences per controller, matching the manual.

The shared service handles source/provider failures, including an expired future Spotify authorization, with the same retry and chime policy. Play the renderer's native Sonos chime when available or a local fallback chime on a reachable compatible renderer. A powered-off or unreachable speaker cannot sound an alarm; expose that failure in global alarm status. Removing a Radio favorite must not invalidate an alarm's canonical station reference. Deleting a custom source referenced by alarms requires an explicit replacement or disabling the affected alarms.

Add an **Alarms** section to the bridge web UI for global management and status. Radio settings can link to it, but do not own alarm records, execution, or reset behavior. Test a small fixture provider representing a future Spotify source to prove that registration, source selection, validation, resolution, execution, and failures require no changes to the scheduler or Radio code. This does not include implementing live Spotify alarms in the Radio release.

Existing Sleep Timer controls remain global playback controls. They must stop Radio or another active source, allow cancellation, and keep the native timer indication consistent. Distinguish playback sleep timer from the controller's own display/power sleep settings.

## Radio settings

Add **Radio Settings** at the bottom of the native Radio menu and a **Radio** entry in the bridge web UI. The web UI provides the full management surface. The controller exposes the settings that fit its native controls, using explicit Apply/change actions; merely browsing a folder must never mutate settings.

| Setting | Default and behavior |
| --- | --- |
| Local Radio location | Saved ZIP/city; offer both original selection flows. |
| Station order | Most Popular; optional By Name where supported. |
| Preferred language | Follow controller language for ranking; language filtering Off. |
| Stream quality | Auto, choosing a compatible reliable stream; offer Best Available and Low Bandwidth when alternative streams exist. |
| Automatic reconnect | On, with bounded attempts and alternate-stream handling. |
| Radio history | On; retain 30 recent distinct stations/episodes and 10 searches; allow Clear History and Off. |
| Episode resume | On for seek-capable episodes. |
| Additional browse entries | Podcasts visible by default; optional By Language root shortcut Off. Preserve the original root entries. |
| Refresh Radio | Refresh directory/metadata without clearing favorites or restarting playback. |
| Reset Radio preferences | Confirm, reset preferences, retain favorites and custom stations. |

Album artwork remains enabled as a requirement. Add an advanced web-only fallback-cover-lookup setting, default On, for users who want to limit supplementary artwork queries. Automatic playback on bridge restart is Off.

The web Radio page also manages favorites/custom stations, exports/imports Radio data, shows the saved location and current stream/metadata source, and provides concise diagnostics for unavailable playback or artwork. Separate display preferences from operations such as Test URL and Clear Cache. Cache clearing must preserve favorites, custom stations, and alarms.

A shortcut from the CR100's System Settings is an optional addition after the Radio Settings entry works. That stock menu is constructed locally. Verify stock native action routes first; if a controller change is necessary to implement genuine Radio controls, scope a small version-specific UI patch. Do not substitute a new Music service or play fake audio items to apply settings.

## Implementation architecture

Build one household Radio subsystem, independent of the selected playback backend:

- `RadioTimeClient`: public OPML/XML/JSON requests, response classification, cache control, timeouts, rate limits, and API errors carried inside HTTP 200 responses.
- `RadioCatalog`: folders, stations, shows, episodes, schedules, search types, popularity/name views, and continuation links.
- `RadioStore`: atomic persistent favorites, custom stations, saved location, preferences, and history/bookmarks. Use stable provider IDs and stable local IDs. Global alarm records belong to `AlarmStore`.
- `NativeRadioService`: the special native Radio discovery/root, original search/location/favorite operations, saved-station/show containers, metadata, and update events. Verify root `0`, `Household.radioUDN`, service identity/account policy, and saved lists such as `R:0/0` before relying on them.
- `RadioPlayback` and `RadioNowPlaying`: explicit live/on-demand routes, selected coordinator, stream alternatives, current metadata generation, and external playback reconciliation.
- Radio artwork resolver/store: reuse the bridge artwork machinery and register it with the actual HTTP artwork handler.
- Radio management API/web page and native settings action adapter: shared validation, explicit writes, consistent live updates, and import/export.
- Shared `AlarmService`, `AlarmStore`, `AlarmScheduler`, provider registry, and backend executors: global management, scheduling, ownership, and fallback. A separate `RadioAlarmProvider` supplies Radio sources and fresh playback resolution; future providers such as Spotify plug into the same contracts.

Native favorite/location mutations need real backend routes and persistence, including update notifications such as RadioFavoritesUpdateID and RadioLocationUpdateID. Existing generic mutation rejection and simulated alarm responses do not constitute support. Browse methods must not gather every upstream page before slicing for a controller request.

Radio configuration changes should apply without restarting the controller's wireless link. Keep data in the installation's normal persistent state directory with atomic writes and serialized concurrent web/controller edits.

## Delivery sequence

1. **Prove the native contracts.** Start a separate isolated emulator instance while the existing emulator is busy. Establish Radio discovery, one root, search types, native location/favorite calls, explicit settings actions, live broadcast semantics, and simultaneous artwork/metadata presentation. Test supported 4.3 and 4.3.2 behavior. Resolve any required narrow UI adaptation here.
2. **Implement catalog and persistence.** Add the public API client, typed catalog, lazy stable browsing, search, location, saved station/show lists, custom stations, and update events. Establish fixture-based error/pagination coverage.
3. **Implement playback with artwork.** Add direct live playback on Sonos and HA, queue preservation, alternate streams, active-room/group metadata, cover resolution, HTTP artwork delivery, and stale-response rejection. Album art is part of this phase's completion criteria.
4. **Complete discovery and management.** Add popular/name views, show listening choices, episodes/schedules, supported resume, history, the controller's Radio Settings entry, and the bridge Radio page with favorite/custom-station management.
5. **Complete global timers and alarms.** Implement the shared alarm service, store, scheduler, provider registry, native/web adapters, and playback executors. Add Radio as the first provider and a fixture provider representing future Spotify support. Verify actual mutations, ownership, persistence, fresh resolution, execution, fallback, snooze/clock behavior, and global sleep-timer compatibility.
6. **Run integrated acceptance.** Verify the whole manual coverage matrix, screenshots, actual backend playback, multiple controllers/rooms, restart recovery, and slow/failing upstream behavior. Document supported formats, known provider-data limits, settings, and operation.

## Acceptance checks

Use deterministic fixtures for transport/state behavior, then a small live API smoke test, isolated emulator screenshots, and actual-device acceptance during implementation. Do not use a full live directory crawl as a routine test.

The user has authorized another emulator instance. Give the Radio test instance its own writable disk/overlay built from an immutable baseline, ports, process/PID files, logs, screenshots, and isolated test-player/network identities. Do not share a writable disk or use an actively changing disk as a backing image. Do not stop, reconfigure, or attach the test bridge to the existing busy instance. Starting the new instance belongs to runtime validation; document editing alone does not require it.

- The original Radio entry and seven original root items remain; Podcasts is visible by default; no new TuneIn music service appears. Native keys, softbuttons, queue access, and room context work.
- All three search types work, including representative host names; results are correctly typed, partial queries work, and no-result/error states remain navigable.
- ZIP and city changes alter both the actual local list and root label; saved locality wins over IP detection and survives restart.
- Browsing beyond the first page produces no duplicate synthetic IDs, cursor loops, eager catalog crawl, invented totals, or repeated/lost actions. Back preserves the selected row. Header/API faults are distinguished from empty data.
- Favorites and custom stations can be added, edited where appropriate, removed, reordered, and restored after restart. Concurrent controllers/web edits converge; deleting a favorite leaves the provider directory unchanged.
- Playing a station preserves a prepopulated queue. Stop/restart, group/volume/mute, external playback takeover, alternate-stream failure, and backend format capabilities behave correctly on Sonos and HA.
- Real matching album art and metadata are visible together on the CR100, in normal and enlarged views. Exercise two successive songs, two stations, program versus song fields, missing/ambiguous art, failed image downloads, reused image URLs, delayed responses, and room/group changes.
- Provider cache controls are honored. Live metadata updates come from actual backend/stream observations, and stale TuneIn metadata cannot overwrite a new song or its image.
- A show with live and archived choices works, as do empty schedules/archives, episode continuation, artwork, correct timezone/DST, finite queue actions, and supported seek/resume.
- Radio settings apply persistently through deliberate actions, leave playback and saved data intact where specified, and update all relevant controllers without radio-link restart.
- Global alarms actually persist and fire with correct room/volume/recurrence/duration, timezone/DST, linked zones at firing time, nine-minute snooze, cancellation, fresh resolution, restart behavior, and unavailable-source chime fallback. Radio favorite deletion leaves alarm sources intact; custom-source removal handles affected alarms explicitly.
- Registering a second fixture content provider, representing Spotify, works through the global web editor and common alarm contracts without changing the scheduler, store, executor interfaces, or Radio code. Verify the native picker separately through its proven generic route or UI adaptation. Authentication failures use the shared fallback policy. Native/bridge delegation does not double-fire, and an expired alarm timer cannot stop playback started manually afterward.
- Global sleep timer stops the current source, including Radio, and can be canceled. The separate emulator instance leaves the existing instance and its writable state untouched.
- Slow/unavailable API, lost renderer, bad stream, malformed response, and missing artwork do not hang navigation or corrupt other rooms' playback/state. Cached favorites remain browsable during directory outages.

## Manual coverage

References use the manual's printed page numbers. Desktop-only workflows are provided by the bridge web UI; native extensions are identified in the feature sections above.

| Manual feature | Pages | Planned surface |
| --- | --- | --- |
| Radio root and worldwide categories | 4-80 to 4-81 | Native Music > Radio and original hierarchy. |
| Stations, Shows, Hosts search | 4-80 to 4-82 | Native Radio > Search. |
| ZIP/city location and Change Location | 4-81 to 4-83 | Native Local Radio and Radio Settings; web equivalent. |
| Station/show favorites and Add to Favorites | 4-81, 4-85 | Separate native favorites lists and persistent add actions. |
| Delete favorite, documented on desktop | 4-85 | Web management; native removal extension where supported. |
| Add/edit custom MP3 station, documented on desktop | 4-83 to 4-84 | Web Add/Edit/Test/Delete and native favorites. |
| Archived shows and podcasts | 4-80 to 4-81 | Visible Podcasts root, native show listening options, and available archives. |
| Artwork, Now Playing, enlarged artwork | 4-19, 4-23 | Native image/metadata display; mandatory per-song art extension. |
| Queue access and native selection preferences | 4-19 to 4-23, 4-81, 7-14 | Preserved queue; finite episode actions; separate live semantics. |
| Zones, grouping, volume, mute | 3-1 to 3-5, 7-6 to 7-7 | Existing room/group controls during Radio. |
| Radio alarm, chime fallback, advanced linked zones | 7-9 to 7-10; 5-4 to 5-5 | Global AlarmClock service and scheduling with a Radio content provider. |
| Show Clock, Stop Alarm, nine-minute Snooze | 7-10 to 7-11 | Native clock/alarm controls and scheduler behavior. |
| Sleep timer | 7-10; 5-6; 6-6 | Native playback timer with cancellation. |

## Scope boundaries and sources

Public RadioTime browsing, playback resolution, local favorites, metadata, and artwork are the integration basis. TuneIn account synchronization and Premium entitlements are separate capabilities and are not assumed. Live rewind/recording and universal song recognition are not provided by directory browsing. Format, archive availability, and seek support follow actual source/backend capabilities.

- The Sonos Controller (CR100) user manual, especially the printed pages in the coverage table.
- [Public RadioTime Music directory](https://opml.radiotime.com/Browse.ashx?c=music&render=json), [Jazz popularity](https://opml.radiotime.com/Browse.ashx?id=c57944&filter=s:popular&render=json), and [station profile example](https://opml.radiotime.com/Describe.ashx?id=s34682&render=json).
- [Archived original RadioTime Browse documentation](https://github.com/skarl/bose-soundtouch-resurrect/blob/main/docs/reference/radiotime/methods/browse.md) and [Describe documentation](https://github.com/skarl/bose-soundtouch-resurrect/blob/main/docs/reference/radiotime/methods/describe.md), mirrored for reference; live behavior takes precedence.
- [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) and [Cover Art Archive API](https://musicbrainz.org/doc/Cover_Art_Archive/API). Supplementary matching uses public metadata/art lookups, a proper client identity, provider limits, and caching.
- Existing bridge architecture and native UI findings: [content.py](../../tools/content.py), [backends.py](../../tools/backends.py), [sonos_backend.py](../../tools/sonos_backend.py), [zoneplayers.py](../../tools/zoneplayers.py), [CONTROLLER-UI.md](../features/CONTROLLER-UI.md), [DIALOGS.md](../features/DIALOGS.md), and [MUSIC-SERVICES.md](../../../../research/bridge-notes/MUSIC-SERVICES.md).
- [Measured Radio directory survey](../../../../scratch/radiotime-survey-2026-11-03.json): observed station counts and local ZIP-area responses, with explicitly bounded scan scope.
