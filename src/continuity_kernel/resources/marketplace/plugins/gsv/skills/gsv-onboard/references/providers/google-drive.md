# Google Drive connection check

Use this note after Google Drive is selected and either its host-owned app or a
Seld-managed `google` connection is available. Never request or handle Google
credentials, authorization codes, tokens, or second factors.

1. Ask for the expected Google account. With a host-owned app, use its read-only
   identity call when available. With Seld-managed auth, the person confirms the
   account during consent and the reader exposes only a stable hashed binding.
2. For Seld-managed auth, call `gsv_connector_source_read` with the exact
   `google` connection ID and source `google_drive`. It reads a small recent
   metadata set, not file bodies. Do not broaden a search merely to prove
   access.
3. Do not create, edit, move, share, download in bulk, delete, or change file
   permissions. Gmail and Calendar share this profile's fixed grant but remain
   separate logical reads and receipts.
4. For a missing host-owned tool, enable the app and open one new task. For
   Seld-managed auth, inspect redacted status and let the person resolve OAuth,
   wrong-account, shared-drive, Workspace, or administrator policy. Never fall
   back to an AI-host or browser session.
5. Return the identity basis, read result, tool shape, horizon, and stable
   references to `$gsv-onboard`, which records only hashed bindings and
   content-free coverage through fresh CAS.
