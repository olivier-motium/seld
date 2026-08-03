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
create/update/move/respond/delete operations plus control of the person's own
CalendarList visibility, colors, reminders, notifications, and subscriptions.
That user-state control uses Google's narrow `calendar.calendarlist` permission;
it does not request the broad `calendar` scope. The command opens Google OAuth
with PKCE and a loopback callback, verifies the returned Google identity, shows
it with the selected access tier, and asks `Use this account? [y/N]` before
publishing. The default is no. A Read connection remains ready while a
same-account Full upgrade is in progress. Older Read grants remain connected;
if one lacks the newer calendar-metadata scope, only that metadata operation
asks for fresh Read consent.

An older single-source Google Calendar Full connection remains usable for every
calendar and event operation its current grant supports. Run
`gsv connectors status google_calendar`; it reports
`calendar_list_control=upgrade_required` when only CalendarList reads are
authorized and returns the exact same-account reconnect command. The existing
connection stays active until the new grant is verified and published. Read
connections report `read_only`; current Full connections report `ready`. A
historical multi-source Google provider bundle is left intact; connect logical
Google Calendar Full separately and confirm the account shown.

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
and event fields, recurrence instances, free/busy, moves, responses, the live
Google color palette, Meet creation, URL attachments, attendee guest counts,
private or shared extended properties, and Google Workspace focus-time,
out-of-office, and working-location events. These additions use the existing
Calendar grants; they do not trigger another OAuth upgrade. Event list
continuations are opaque: keep using the returned continuation until the last
page rotates it to a new sync cursor. An expired cursor reports
`full_sync_required`; restart that one list without its old cursor. Filters on
extended properties cannot be replayed with a sync cursor, so filtered lists return
only ordinary page continuations. `show_hidden_invitations` remains replayable
when the same value accompanies every sync request.

`calendars.list` continues to return the user's CalendarList entries with sync
continuations. Use `calendar_list.get` for one complete entry and
`calendar_list.insert`, `calendar_list.update`, or `calendar_list.remove` for
that person's list state. These are intentionally separate from
`calendars.get/create/update/delete`, which act on the shared Calendar resource.
Hiding a CalendarList entry with `hidden=true` is the reversible everyday way to
remove it from view.

CalendarList insert and update support a provider-validated foreground/background
RGB pair, `hidden`, `selected`, a custom summary, complete default-reminder
replacement, and complete email-notification-type replacement. Supplying a color
pair sends Google's required `colorRgbFormat=true`. Reminder and notification
arrays replace the entire corresponding provider array. Adding entries or
reordering them is one-step; dropping an existing entry or clearing a non-empty
custom summary is destructive and requires confirmation. Seld reads the exact
CalendarList ETag and current preference arrays again before PATCH, refuses a
stale version, and sends `If-Match`.

`calendar_list.remove` removes only a non-owned, non-primary entry from this
person's list. It does not delete the Calendar or its events, so it is
destructive rather than permanent. It does lose the saved view preferences;
`calendar_list.insert` can add the calendar again only while access remains and
does not promise to restore those preferences. Removal therefore requires the
reviewed CalendarList snapshot plus its ETag and is re-read immediately before
DELETE. Google does not let a data owner remove their own CalendarList entry;
hide it instead. An `owner` access role alone is not data ownership, so an
owner-level shared entry remains removable when its separate `dataOwner` is
someone else. CalendarList `watch` is not exposed because it requires a separate
webhook-channel lifecycle and there is no current consumer.

Calendar and event `update` operations are partial PATCHes. Supplying attendees,
attachments, or recurrence replaces that complete array. Attendee or attachment
removal, a lower `additional_guests` count, recurrence replacement, Meet
replacement, and an extended-property value of `null` are destructive.
Attendee additions and shared-event changes are outward. Reminder-only changes
remain local. Ordinary single events may use an RFC3339 offset without a
separate time zone, while recurring events, including all-day series, need one
matching explicit zone. All-day end dates are exclusive. Optional
client-supplied event IDs use Google's base32hex alphabet (`0-9`, `a-v`) and
must be 5-1024 characters.

Use `colors.get` to read the account's current event and calendar color IDs;
do not assume a static palette. To ask Google to create a Meet conference,
supply a fresh caller-owned `meet_request_id`. Seld sends
`conferenceDataVersion=1` and returns Google's `pending`, `success`, or
`failure` state as received. It does not poll or retry a pending create request.
Re-read the event later if the person wants the final conference state.

An event accepts at most 25 attachments. Supply each exact absolute HTTP or HTTPS
`file_url`; Seld validates and forwards the URL but never fetches it, looks up
Drive metadata, or changes file permissions. The Calendar connection therefore
needs no Drive scope, while recipients still need whatever access the linked
file itself requires. Private and shared extended-property keys are at most 44
characters, values are at most 1,024 characters, and the submitted sets are
bounded to 300 properties and 32 KiB combined. Updates use an explicit `null`
value to delete one property.

Special status events use the same `events.create` and `events.update`
operations rather than a parallel tool family. On create, set `event_type` to
`focusTime`, `outOfOffice`, or `workingLocation` and supply its matching typed
properties object. Seld adds Google's required opacity for focus time and out of
office, and public/transparent visibility for working location. When create
omits auto-decline or focus-time Chat status, Seld explicitly sends
`declineNone` and `available`; an undocumented provider default cannot add an
external effect behind a one-step change. Updates first read the immutable
provider `eventType` and reject a properties object for a different type before
writing. Reads and `event_types` filters already return the provider's
corresponding typed properties without projection.

Google offers these status types only on a primary calendar and only to
eligible Workspace accounts, so use `calendar_id: primary`. Account edition,
administrator policy, and whether Chat is enabled remain provider-owned. An
unsupported-account response does not mean the OAuth connection is stale and
reconnecting does not add a missing product entitlement. Focus time and out of
office must be timed. All-day working locations span exactly one day; timed
ones may span normally. A focus-time `chat_status` of `doNotDisturb` also needs
a block under 24 hours.

Working location is public, so creation and location or schedule changes need
an outward confirmation. Non-`declineNone` auto-decline rules can respond to
new or existing conflicting invitations; a decline message and an explicit
focus-time Chat status are also shown in an outward preview. Disabling
auto-decline and changing only a private local preference remain one-step.
Deleting a status event or moving its time interval does not promise to reverse
invitations Google already declined.

Birthday events also use `events.create` and `events.update`. Create one with
`event_type: birthday`, a one-day all-day start/end pair, and optionally only a
summary, color, or reminders. Seld fixes the provider-owned shape to private
and transparent with annual recurrence. A February 29 birthday uses Google's
last-day-of-February recurrence rule. It does not send `birthdayProperties`,
because Google's optional fixed `birthday` type adds no user choice.

Birthday updates first read the live `eventType` and may change only the
summary, color, reminders, or one-day all-day date pair. A date linked to Google
Contacts must be edited in Contacts; the account owner's date must be edited in
the Google Account profile. Deleting a Calendar birthday does not change either
source. These operations use the existing Calendar grant and do not require a
new OAuth scope. A birthday on `primary` is private and one-step. Creating one,
or changing its summary, color, or date on another calendar, requires outward
confirmation; a reminder-only change remains a one-step local preference.

Events generated automatically from Gmail are provider-owned
`eventType: fromGmail` events. Seld can read, filter, update, RSVP to, and
delete an existing one, but it cannot create one or move it to another
calendar. Google controls whether the account and region generate these events;
reconnecting does not enable Gmail smart features or create a missing event.

For `events.update`, Google permits only color, reminders, visibility,
transparency, `confirmed` or `tentative` status, attendees, and private or
shared extended properties. Seld reads the live type and ETag before PATCH and
rejects schedule, title, description, location, recurrence, attachment, Meet,
guest-policy, or status-property changes before writing. Use `events.delete`,
not an update with cancelled status, to remove one; that keeps deletion on the
snapshot-bound destructive path with an explicit `send_updates` choice.

Color, reminders, and non-deleting private properties on `primary` are
copy-private one-step changes. Visibility, transparency, non-deleting status,
shared properties, notifying updates, and attendee changes require outward
confirmation. Removing an attendee, reducing additional guests, or deleting an
extended property is destructive. Supplying attendees replaces the complete
array; preserve each attendee's comment, display name, optional/resource flag,
and response status when it should remain. The email-only attendee shorthand is
refused for these events because it would discard that metadata. `events.respond`
works only when the fresh read identifies exactly one self attendee. These
behaviors use the existing Calendar grant and need no OAuth upgrade.

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
with `resource_changed_reread_required`. For CalendarList update or removal, use
the CalendarList ETag returned by `calendar_list.get`. Normalize the preceding reads to the
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

Return only the identity basis, bounded-read result, covered interval, and
stable references to `$gsv-onboard`. Do not persist provider bodies, raw account
identifiers, OAuth material, or error text.
