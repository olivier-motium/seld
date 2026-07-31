# Google Calendar

Use this note after the person selects Google Calendar. A host-owned app may
satisfy the bounded Pulse recipe. Portable Seld custody uses its own logical
Calendar connection and never borrows another host's session.

## Connect

```text
gsv connectors connect google_calendar --access read --browser firefox
gsv connectors connect google_calendar --access full --browser firefox
```

Read grants calendar, event, instance, and free/busy reads. Full adds calendar
and event create/update/move/respond/delete operations. The command opens Google
OAuth with PKCE and a loopback callback, verifies the returned Google identity,
shows it with the selected access tier, and asks `Use this account? [y/N]`
before publishing. The default is no. A Read connection remains ready while a
same-account Full upgrade is in progress.

The person owns account selection, consent, Workspace policy, administrator
approval, and second factors. If the installed build lacks a public Google
client registration, sign-in stops before OAuth and saves nothing.

## Verify the source

Use `gsv connectors status google_calendar`, confirm the expected account and
calendar set, then call `gsv_connector_source_read` with that exact connection
ID and source `google_calendar`. Read one bounded current event window and keep
its covered interval separate from observation time. An empty successful window
counts. This verification is intentionally bounded because it proves Pulse
coverage; it is not the interactive feature boundary.

The isolated `gsv_connectors` server exposes `gsv_google_calendar_read` and
`gsv_google_calendar_write`. Their closed schemas cover the ordinary calendar
and event fields, recurrence instances, free/busy, moves, and responses. Event
deletion is destructive, while whole-calendar deletion is permanent. An event
mutation becomes
outward when it notifies or affects attendees, and then requires an exact
preview plus short-lived bound confirmation. Permanent provider-side effects
remain separately classified.

Return only the identity basis, bounded-read result, covered interval, and
stable references to `$gsv-onboard`. Do not persist provider bodies, raw account
identifiers, OAuth material, or error text.
