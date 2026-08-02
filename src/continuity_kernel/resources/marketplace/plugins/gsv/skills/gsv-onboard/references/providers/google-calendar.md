# Google Calendar

Use this note after the person selects Google Calendar. A host-owned app may
satisfy the bounded Pulse recipe. Portable Seld custody uses its own logical
Calendar connection and never borrows another host's session.

## Connect

```bash
gsv connectors connect google_calendar --access read --alias 'Family Calendar'
gsv connectors connect google_calendar --access full --alias 'Family Calendar'
```

Read grants CalendarList, calendar metadata, event, instance, and free/busy
reads with separate least-authority scopes. Full adds calendar and event
create/update/move/respond/delete operations. The command opens Google OAuth
with PKCE and a loopback callback, verifies the returned Google identity, shows
it with the selected access tier, and asks `Use this account? [y/N]` before
publishing. The default is no. A Read connection remains ready while a
same-account Full upgrade is in progress. Older Read grants remain connected;
if one lacks the newer calendar-metadata scope, only that metadata operation
asks for fresh Read consent.

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
list continuations are opaque: keep using the returned continuation until the
last page rotates it to a new sync cursor. An expired cursor reports
`full_sync_required`; restart that one list without its old cursor.

Calendar and event `update` operations are partial PATCHes. Supplying attendees
or recurrence replaces that complete array. Attendee removal and recurrence
replacement are destructive; attendee additions and shared-event changes are
outward. Reminder-only changes remain local. Ordinary single events may use an
RFC3339 offset without a separate time zone, while recurring events, including
all-day series, need one matching explicit zone. All-day end dates are
exclusive. Optional
client-supplied event IDs use Google's base32hex alphabet (`0-9`, `a-v`) and
must be 5-1024 characters.

Move, RSVP, event deletion, and whole-calendar deletion require the reviewed
event or CalendarList snapshot returned by the preceding read. The connector
shows that human target in the preview, re-reads it, compares the ETag and
snapshot, and refuses stale authority. Moving an event changes its organizer
and works only for default events. Whole-calendar deletion is limited to an
owned secondary calendar and is permanent for everyone. Event deletion is
destructive and normally recoverable from Google Calendar Trash for a limited
period, with different recurrence behavior. Choose `send_updates` explicitly
for moves, deletion, and attendee changes; `none` can leave external calendars
stale and does not guarantee that Google sends no email.

For whole-calendar deletion, use the Calendar resource ETag returned by
`calendars.get`; a CalendarList entry has a different ETag and will fail closed
with `resource_changed_reread_required`. Normalize the preceding reads to the
closed confirmation shapes below rather than copying their extra provider
fields. Keep every listed key, using `null` where the provider omitted a
nullable event or calendar field. Within `organizer`, keep `displayName` and
`self` exactly when the read returned them; omitting a returned field is a
normalization error, not provider drift.

```json
{
  "expected_calendar": {
    "accessRole": "owner",
    "id": "secondary-calendar-id",
    "primary": false,
    "summary": "Team calendar"
  },
  "expected_event": {
    "end": {"dateTime": "2026-08-02T10:00:00+02:00", "timeZone": "Europe/Brussels"},
    "etag": "provider-event-etag",
    "eventType": "default",
    "id": "provider-event-id",
    "organizer": {"displayName": "Owner", "email": "owner@example.com", "self": true},
    "start": {"dateTime": "2026-08-02T09:00:00+02:00", "timeZone": "Europe/Brussels"},
    "status": "confirmed",
    "summary": "Planning"
  }
}
```

Drive attachments are not exposed by a separate Calendar connection because
that grant deliberately has no Drive authority. A later cross-connector flow
may add them only after it can bind a separately verified Drive connection;
Calendar OAuth never broadens silently.

Return only the identity basis, bounded-read result, covered interval, and
stable references to `$gsv-onboard`. Do not persist provider bodies, raw account
identifiers, OAuth material, or error text.
