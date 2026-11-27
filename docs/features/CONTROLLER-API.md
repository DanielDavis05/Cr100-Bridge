# Controller JSON API

The bridge exposes a versioned JSON API for paired controllers running RE100 R20
or later. Open **Password → Controller API tokens** in the bridge web UI to create
or revoke a named token. Copy the token when it appears. Each token can discover
and control every paired CR100 and is scoped to these controller API endpoints.

Open **Swagger docs** in that panel, or visit `/swagger/` on the bridge. Choose
**Authorize** and paste the token itself. The OpenAPI schema is at
`/api/v1/openapi.json`. The bridge serves its own copy of
[Swagger UI 5.36.0](https://github.com/swagger-api/swagger-ui/releases/tag/v5.36.0).

| Method and path | Purpose |
| --- | --- |
| `GET /api/v1/controllers` | List paired controller MAC addresses, names, and recent command readiness |
| `GET /api/v1/controllers/{mac}/remote` | List actions, menu names, and room IDs visible to one controller |
| `POST /api/v1/controllers/{mac}/remote` | Send one command to a paired controller |
| `POST /api/v1/controllers/all/remote` | Send the same command to every paired controller and return one result per controller |

Supply `Authorization: Bearer <token>` with each API call. Send
`Content-Type: application/json` with commands. Keep this credential on a trusted
network or use an HTTPS endpoint you control when sending it across other networks.

```sh
BRIDGE_URL=http://192.168.1.50:8080
CR100_MAC=00:0e:58:12:34:56
# Set CR100_TOKEN in the calling environment before running these examples.

curl -sS -H "Authorization: Bearer $CR100_TOKEN" \
  "$BRIDGE_URL/api/v1/controllers/$CR100_MAC/remote"

curl -sS -H "Authorization: Bearer $CR100_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"now_playing","room":"ha:media_player.kitchen"}' \
  "$BRIDGE_URL/api/v1/controllers/$CR100_MAC/remote"

curl -sS -H "Authorization: Bearer $CR100_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"wake"}' "$BRIDGE_URL/api/v1/controllers/all/remote"
```

The other action names are `rooms`, `music`, `settings`, and `find`. Use the
room ID from the discovery response for `now_playing`. If the room is hidden from
one of the targets in an `all` request, the other controllers still receive the
command.

A single-controller request waits up to eight seconds for a receipt. An `all`
request returns HTTP 200 with a result for each controller, including any that
timed out or rejected the command. Its top-level `ok` is true when every result
succeeded. Each result has its own `ok` and `status`. Failures also carry a
machine-readable `code` and a human-readable `error`. HTTP 404 with
`no_controllers` means there were no paired targets.
