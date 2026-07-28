# Google Drive connection check

Use this note after Google Drive is selected and its app tools are present in a
fresh ChatGPT task. The separately installed app owns OAuth and account scope.

1. Ask for the exact expected Google account. Use a read-only profile call when
   available; do not infer Drive identity from Gmail or Calendar.
2. Search or read a small recent file set, or the one named file the person
   selected. Keep previews bounded and do not broaden a search to prove access.
3. Do not create, edit, move, share, download in bulk, or delete files and do
   not change permissions during onboarding.
4. For missing tools, enable the app and open one new task. For authentication,
   wrong-account, shared-drive policy, or admin failures, stop and use the
   provider-owned connection settings before one changed retry.
5. Return the observed identity, read result, tool shape, horizon, and stable
   references to `$gsv-onboard`, which records only hashed bindings and
   content-free coverage through fresh CAS.
