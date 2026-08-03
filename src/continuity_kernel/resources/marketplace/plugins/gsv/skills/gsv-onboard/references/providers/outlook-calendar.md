# Outlook Calendar

Use this note after the person selects Outlook Calendar. A host-owned Outlook
app may satisfy the bounded Pulse recipe. Portable Seld custody uses its own
logical Calendar connection and never borrows another host session.

## Connect

```bash
gsv connectors connect outlook_calendar --access read --alias 'Work Calendar'
gsv connectors connect outlook_calendar --access full --alias 'Work Calendar'
```

Read grants calendar, event, window, instance, free/busy, and attachment reads.
Full adds calendar and event CRUD, accept/tentative/decline/cancel/forward, and
attachment mutations. Shared and delegated calendar access is not silently
included in either tier. The command opens Microsoft OAuth with PKCE and a
loopback callback, verifies the Graph identity, shows the account and chosen
tier, and asks `Use this account? [y/N]` before publishing. The default is no.
A Read connection remains ready during a same-account Full upgrade.

The person owns account and tenant selection, consent, administrator approval,
conditional access, and second factors. If the installed build lacks a public
Microsoft client registration, sign-in stops and saves nothing.

## Verify the source

Use `gsv connectors status outlook_calendar`, confirm the expected account,
tenant, and calendar set, then call `gsv_connector_source_read` with the exact
connection ID and source `outlook_calendar`. Read one bounded current event
window and keep its covered interval separate from observation time. An empty
successful window counts. This proves Pulse coverage; it is not the feature
boundary for interactive Calendar work.

The isolated `gsv_connectors` server exposes `gsv_outlook_calendar_read` and
`gsv_outlook_calendar_write`. Window and recurring-instance reads accept an
explicit response time zone and page size through 1,000; Graph continuations
remain bound to the exact calendar, event, and original start/end window.
Local tests prove that forged or altered window continuations fail closed. A
provider-backed release canary must still observe and replay a real second-page
Graph continuation before claiming live pagination proof for a release.
Free/busy accepts at most 20 schedules per Microsoft request and an optional
5-1440 minute availability interval. Microsoft does not support `getSchedule`
for personal Microsoft accounts; Seld reports that account-or-permission
boundary without retrying it as a transient error.

Event creation supports up to Microsoft's documented 500 attendees and can
create a Teams meeting with `is_online_meeting: true`; Teams for Business is
the default provider. Attendee-affecting creates and updates, responses, and
forwards require an outward confirmation. Event delete and cancel keep the
stronger destructive confirmation and explicitly warn that Outlook can send
attendee cancellation mail. Existing-event updates, attachment adds, event
deletes, and permanent event purges re-read the event and require the exact
current `change_key` both at preview and dispatch. Calendar deletion and purge
first read the calendar and reject both the `primary` alias and a real Graph
calendar ID whose `isDefaultCalendar` flag is true. Microsoft exposes no v1.0
restore action for a deleted calendar or event, so Seld does not promise an
agent-executable recovery path; permanent purge remains a separate
confirmation.

A local-file `attachments.add` is snapshot-bound and promoted to an outward
confirmation: preview does not write to Outlook, and a matching replay
dispatches the pinned immutable file once. Calendar/event preconditions and
the safe-versus-outward classification are rechecked on replay.

Attachment uploads follow Microsoft's full-transfer contract. A file smaller
than 3 MB (3,000,000 bytes) uses one direct attachment POST, including
zero-byte files. Files from 3 MB through 150 MiB use `createUploadSession` and
sequential 3 MiB chunks to the exact preauthenticated
`https://outlook.office.com` URL, without an Authorization header on those
PUTs. 150 MiB is this connector's hard provider ceiling; tenant, mailbox,
policy, and service limits can still reject a smaller upload. Shared or
delegated large attachment sessions may also hit Microsoft's documented known
issue; this connector does not hide or retry that failure.

Attachment metadata reads select concrete fields without materializing
`contentBytes`. Request `delivery: "artifact"` for an attachment read to
stream `/$value` into the owner-only transient artifact store and receive a
bounded artifact receipt. Outlook MIME and attachment artifact downloads stop
at the connector's 150 MiB Outlook ceiling before they can consume unbounded
local disk. Attachment reads are now metadata-only by default; callers that
previously consumed inline `contentBytes` must request `delivery: "artifact"`.

Return only the identity basis, bounded-read facts, covered interval, and
stable references to `$gsv-onboard`. Do not persist event bodies, raw account
or tenant identifiers, OAuth material, or provider error text.
