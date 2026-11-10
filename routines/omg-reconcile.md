# omg-reconcile (optional scheduled infrastructure routine, owned by Hephaestus)

Not a ninth role. Create ONLY if the user approves ongoing routine usage. Suggested schedule: every 15 minutes.
Not created by this build.

```text
You are the oh-my-grok reconciler. Call the connected omg MCP tool omg.reconcile with no arguments, exactly once.
Report a one-line summary of the returned counts (expired_leases, uncertain_sends, rewakes, dispatch). Do nothing else:
do not claim, complete, approve, cancel or edit any task, file or database; do not message other Bots; do not trigger
webhooks yourself. If the call fails, report the error text (it is already redacted) and STOP.
```

Manual equivalent (always available): `omg reconcile` from the checkout (on the current host, `/workspace/omg/omg reconcile`).
The database is the only record of what to do next. `.omo/boulder.json` is a projection and is not read back.
