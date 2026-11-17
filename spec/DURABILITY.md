# DURABILITY (sibling to `spec/IMPLEMENTATION.md`)

Status of this document: slices **1, 2 (code only), 3, 4, and 5** are implemented on this branch. Slice 0 (a live agent-store probe), slice 6 (the routine that asks for a backup), and slice 7 (skill and kit mirrors) are **not** in this change.

`spec/IMPLEMENTATION.md` is **not edited**. Its SHA-256 remains

`9643ee06f6817b08c57ea585fd8f919d4b23639bffd817a4a3084c7c9d0d30ae`

(`tests/unit/test_spec_integrity.py` asserts those exact bytes, including from this document's pin). This file is the sibling that describes how omg state survives a wiped box, a moved box, or a restore onto an older snapshot.

No plaintext secret leaves the box. A backup is a tar of state, encrypted to a public key, then signed. The encryption private key is passphrase-protected and lives in the recovery kit. The signing private key stays on the box and has no passphrase, so an hourly run can sign without a prompt.

## 1. What a bundle holds

One bundle is one consistent snapshot:

| Member | Source | Notes |
|---|---|---|
| `state/omg.sqlite3` or `state/omo.sqlite3` | the live database, via the SQLite online backup API | exactly one database file; `PRAGMA integrity_check` must be `ok` before the file is packed |
| `config/policy.json` | the untracked policy file | omitted when the file is absent; never committed by this change |
| `jobs/` | the jobs tree | full tree; symlinks are refused |
| `state/logs/` | the log tree | full tree; symlinks are refused |
| `state/secrets/hooks.json` | raw bytes | omitted when absent; never parsed, logged, printed, or copied into an error |
| `manifest.json` | written by the backup | see below |

The manifest is JSON with:

- `bundle_format` (this writer uses `1`)
- `schema_version` (the SQLite schema version of the snapshot)
- `omg_version`
- `git_sha` (`git rev-parse HEAD`, or `unknown`)
- `created_at` (UTC, the same clock format as `db.utcnow`)
- `mcp_command`, exactly `<repo>/.venv/bin/python <repo>/oh_my_grok/mcp_server.py`
- `generation`: `max_event_id` plus `tail_sha256`, the SHA-256 of the canonical JSON of the last 50 events in id order
- `environment`: SHA-256 of `requirements.lock` and `tooling/requirements-tools.lock` when those files exist
- `files`: per archived file, relative path to `{sha256, bytes, mode}`

`manifest.json` is not an entry of `files`. After decrypt, every other file's bytes must match `files`, and the archive must not contain an extra file, a link, or a path that escapes the tree.

The recorded generation identifies the snapshot. The restore **decision** compares the full ordered event streams of the local database and the snapshot (section 6), which is stricter than comparing the two tuples alone.

`state/backups/`, the recovery kit, and the on-box signing key are not inside the bundle.

## 2. Feature flag

`policy.json` key `features.backup`, modes `off | on`, default `off`. It is normalized with the other flags, so a job frozen at submit records it, but the watchdog and doctor read the **current** policy (same rule as heal collect). Turning it on does not change `healing`, `watchdog`, `boulder`, or any other flag, and does not change their defaults.

| Mode | Hourly step | Doctor checks |
|---|---|---|
| `off` | skipped (`reason` `backup off`) | `backup_age`, `backup_offbox`, `backup_venv_tools` report `SKIP` and do not fail doctor |
| `on` | a bundle is written when the newest local bundle is older than `backup_interval_seconds` (default 3600). A newer bundle is not rebuilt; a configured target that lacks it is pushed again | see section 5 |

An explicit `omg backup` (no `--out`) builds a bundle whether the flag is off or on. The flag gates the unattended hourly step and the doctor checks, not the operator command. `omg backup --out PATH` is unchanged: it still writes one integrity-checked SQLite file and does not encrypt.

Policy keys added for this feature (defaults in parentheses):

- `backup_interval_seconds` (3600)
- `backup_max_age_seconds` (7200)
- `backup_keep_hourly` (48)
- `backup_keep_daily` (14)
- `backup_size_cap_bytes` (1073741824)
- `backup_targets` (`[]`)

Unknown target types, unknown target keys, and a target that names the public `pysol-dev/oh-my-grok` repository are refused when the policy is loaded.

## 3. Keys (`omg backup init-key`)

One-time. A second run exits conflict and does not overwrite. `--kit` chooses the recovery kit directory (default `state/recovery-kit`). The chosen absolute path is recorded in `state/secrets/backup-kit.path` (mode `0600`). Later publication and local kit lookup read that path. A symlink kit directory is refused.

The passphrase is read only from the environment variable `OMG_BACKUP_PASSPHRASE`. There is no passphrase argument. The value is never logged, never placed on a command line, and is registered with the redactor. It must be a single non-empty line of at least 12 characters.

The command:

1. Generates an encryption key (ECDSA P-256 primary, ECDH P-256 subkey) inside a mode `0700` temporary GnuPG home. The passphrase is supplied to gpg on `--passphrase-fd` through a pipe. It is not written to a file.
2. Exports the encryption **public** key and the **passphrase-protected** secret key into the recovery kit (`state/recovery-kit/` unless `--kit` is set): `encryption.pub`, `encryption.sec.asc`, mode `0600`, directory mode `0700`.
3. Shreds the temporary home. The plaintext encryption private key is not written anywhere else.
4. Generates a separate Ed25519 signing key with no passphrase **in** `state/secrets/backup-gnupg/` (mode `0700`). `signing.pub` and `signing.fpr` (the fingerprint) are copied into the kit. The same fingerprint is stored in `state/secrets/backup-signing.fpr`. The signing secret is not exported to a file. When that directory's path is too long for an `AF_UNIX` socket, `gpg` is pointed at a private short symlink to the same directory; the key files stay under `state/secrets/backup-gnupg/`.

The passphrase is given to gpg with `--passphrase-fd` and `pinentry-mode loopback`. No passphrase file is created.

The on-box keyring used for hourly backups contains the encryption public key and the signing secret. It does not contain the encryption secret. Decrypting a bundle requires the kit plus the passphrase.

## 4. `omg backup` and off-box targets

`omg backup` writes `state/backups/bundles/omg-YYYYmmddTHHMMSSffffffZ.tar.gpg` and a detached `.sig` over the ciphertext. Both are mode `0600`. A second bundle in the same microsecond is `...Z-0002.tar.gpg` with a zero-padded sequence, and newer sequences sort after older ones. The tar is encrypted to the encryption public key (no passphrase) and signed with the on-box signing key.

`omg backup push` publishes the newest local bundle. `omg backup` pushes too, unless `--no-push` is set. `omg backup --out PATH` does not push.

Targets come from `backup_targets`. Two types are implemented. Tests use local fakes only; this slice does not contact a real GitHub repository or a real agent store.

### 4.1 Agent-store directory

`{"type": "agent_store", "path": "..."}`. `path` is optional. When it is omitted, `OMG_BACKUP_AGENT_STORE` is used if set, otherwise `/cursor/stores/user/omg-backups`.

Layout:

```
<path>/
  bundles/<name>.tar.gpg
  bundles/<name>.tar.gpg.sig
  bundles/<name>.public.json
  kit/encryption.pub
  kit/encryption.sec.asc
  kit/signing.pub
  kit/signing.fpr
  LATEST
```

`LATEST` names the newest bundle file. The kit copy is the recovery kit (public encryption key, protected encryption secret, signing public key, and the pinned signing fingerprint). The signing secret is not copied.

`<name>.public.json` is the only manifest that leaves the box. Its keys are exactly `bundle`, `sha256` (of the ciphertext), `created_at`, `generation` (`max_event_id` and `tail_sha256`), and `signature` (the signature filename). It has no paths, no file list, and no hook or policy metadata. The full `manifest.json` stays inside the encrypted bundle. A local `<name>.manifest.json` may sit next to the ciphertext on the box so doctor can compare lockfiles without the passphrase; that file is not copied to the agent store or uploaded as a release asset.

Retention, applied to the local bundle directory and to each agent-store `bundles/` directory:

- Keep the newest bundle of each UTC day for the newest `backup_keep_daily` days (default 14), as daily anchors.
- Also keep the newest `backup_keep_hourly` bundles (default 48).
- The union of those sets is kept.
- If the kept files (ciphertext plus signature) exceed `backup_size_cap_bytes`, drop the oldest kept bundle that is not the single newest, preferring a bundle that is not a daily anchor. The newest bundle is never deleted, even when it alone exceeds the cap.

### 4.2 Private GitHub repository

`{"type": "github_release", "repo": "owner/name", "checkout": "...", "mirror": "...", "remote": "..."}`. `repo` is required and must be exactly `owner/name`. A URL or an scp path in `repo` is rejected. `checkout` is optional (default `state/backups/github/<owner>__<name>`, mode `0700`). `mirror` is an optional local directory of already-downloaded assets, used by tests and by doctor so a check does not have to call `gh`. `remote` is optional and defaults to `https://github.com/<repo>.git`. It may only be a local filesystem path, a `file://` URL, or an `https` or `ssh` URL whose host is exactly `github.com`. A local filesystem path is not parsed as a GitHub URL and is passed to git unchanged. Trailing dots on the host are stripped. Path segments are split on raw slashes and then percent-decoded, and must be exactly `/owner/name[.git][/]`. `.`, `..`, empty segments, and a segment that decodes to a slash (`%2f`, `%2F`) are rejected, including `%2e%2e`. A semicolon (`;params` or a trailing `;`), a question mark, and a hash are rejected even when the query or fragment is empty. `http`, `git`, `persistent-https`, scp-like `git@host:path`, and a schemeless `github.com/owner/name` are refused. A `file://` URL is accepted only with an empty or `localhost` host, no user, password, or port, and an absolute path with no `.`, `..`, or empty segment. `file:path` without `//` is refused, because git reads it as ssh to a host named `file`. Host aliases, IP literals, `www.github.com`, and `ssh.github.com` fail closed. Tests set `remote` to a local bare repository.

Git is not given the configured string. An https remote is pushed as `https://github.com/<repo>.git`. An ssh remote is pushed as `ssh://git@github.com/<repo>.git`. A file URL is pushed as `file://` plus that absolute path. Kit git commands run with `-c core.hooksPath=/dev/null`, `GIT_CONFIG_GLOBAL=/dev/null`, and `GIT_CONFIG_NOSYSTEM=1`, and without a parent `GIT_CONFIG_COUNT` or `GIT_CONFIG_PARAMETERS`. If the checkout's own config contains `insteadOf` or `pushInsteadOf`, the push is refused and does not exit 0. `git remote get-url --push origin` must equal the rebuilt URL. A different `remote.origin.pushurl` is removed; if the push URL still differs, the push is refused.

Before every commit, push, and release upload, publication runs `gh api repos/<owner>/<name>` and continues only when `visibility` is `private`. `internal` is refused. A failed query or any other visibility fails closed and does not commit, push, or upload. The check runs again on every push. The remote is checked against `repo` before that query, so a private `repo` cannot push its kit to a different repository.

Git stores **only** the recovery kit (`kit/` plus a README that points at release assets). Bundles are release assets. The tag is the bundle name without `.tar.gpg`. A missing release is created with `gh release create`. A release whose ciphertext or signature is missing, or whose size or sha256 does not match the local file, is repaired with `gh release upload --clobber` (that flag replaces an asset; it is not a git force-push). After the upload, the assets are read back. A mismatch is a non-zero failure. Retention deletes older releases and their tags (`gh release delete --yes --cleanup-tag`). Nothing in this path uses `--force`, `--force-with-lease`, `-f` on `git push`, or a refspec that starts with `+`. A rejected push is a failure. It is not retried with a force. The next publication pushes `HEAD:main` again even when the kit files are already committed, so a commit that never reached the remote is not skipped.

An `https` or `ssh` URL whose host is exactly `github.com` and whose owner/name is `pysol-dev/oh-my-grok` is refused before a git or `gh` command runs. That includes case differences, a trailing dot on the host (`github.com.`), a trailing slash, a `.git` suffix, `https://user@github.com/...`, and `ssh://git@github.com:22/...`. A path that is not exactly `/owner/name[.git][/]` is refused, so `../..`, `%2e%2e`, `%2f`, `.git/../../`, a semicolon, and an empty `?` or `#` cannot be collapsed onto the public repository. Scp-like `git@github.com:owner/name.git`, `http`, `git`, `persistent-https`, a schemeless `github.com/owner/name`, `www.github.com`, `ssh.github.com`, an SSH host alias, and an IP literal are refused as well.

## 5. Watchdog and doctor

The watchdog tick always records a `backup` step.

- `backup=off`: the step is skipped. This does not change reconcile, heal collect, or stall behaviour.
- `backup=on`: `hourly_backup` runs. A bundle newer than `backup_interval_seconds` is kept. If a configured target is missing that bundle, the tick pushes it again. Otherwise a new bundle is created and pushed. A failure is isolated to this step; the rest of the tick still finishes.

Doctor, when `backup=on`:

| Check | Fails when |
|---|---|
| `backup_age` | there is no local bundle, or the newest bundle's filename timestamp is older than `backup_max_age_seconds` (default 7200, two hourly intervals, so one late tick does not flap) |
| `backup_offbox` | a configured target does not have the newest local bundle and its signature. No targets configured is a pass, with that fact in the detail. Agent-store targets are checked on disk. A GitHub target with `mirror` is checked on disk. Without `mirror`, doctor asks `gh` and does not force-push |
| `backup_venv_tools` | `.venv/bin/python` is missing, or `requirements.lock` / `tooling/requirements-tools.lock` no longer match the hashes stored in the newest bundle's manifest. The full manifest is not decrypted; a local `<name>.manifest.json` (mode `0600`, on the box only) lets doctor compare without the passphrase. A git SHA difference is reported in the detail and does not by itself fail the check |

When `backup=off` those three checks are `SKIP` with `ok` true. Existing doctor output stays OK.

`omg backup status` and the MCP tool `backup_status` report the flag, the newest local bundle name, its age, and the configured target types. They do not read `hooks.json` and they do not create a bundle.

## 6. `scripts/recover.sh`

Plain bash. The archive and SQLite steps run under system `python3 -m oh_my_grok.recover` (stdlib plus gpg). They do not use the project venv. `./omg` is used only after a successful restore decision, and only when `--check` was not passed.

Order:

1. Require `python3`, `gpg`, and `OMG_BACKUP_PASSPHRASE` (present and non-empty; the value is never printed).
2. Unless `--check`: run `scripts/bootstrap.sh`.
3. Locate the newest bundle. Candidates are collected from `OMG_BACKUP_AGENT_STORE`, each configured agent-store path, `/cursor/stores/user/omg-backups` when that directory exists, the local bundle directory, `OMG_BACKUP_GITHUB_MIRROR`, a configured GitHub `mirror`, and, when `OMG_BACKUP_GITHUB_REPO` is `owner/name`, a `git clone` plus `gh release download`. The newest timestamp wins. The same timestamp prefers the higher sequence suffix, then the agent store over the local copy over a GitHub mirror. A GitHub release that does not have both a non-empty ciphertext and a non-empty signature is skipped, and the next newest complete release is used. The clone is not a force. The public repository is refused. A bad signature or a manifest mismatch on the chosen bundle still exits 12 and does not fall through to an older bundle.
4. Verify the detached signature. `OMG_BACKUP_KIT`, when set, is the kit used even if the bundle's source already has a complete kit. An incomplete or symlinked override fails closed. Otherwise the kit beside the chosen bundle is used, then the other kit locations. The signer fingerprint must equal the pin recorded at `init-key` (`signing.fpr` in the kit, and `state/secrets/backup-signing.fpr` when the box still has it). Those two pins must agree when both exist. A `signing.pub` whose fingerprint differs from the pin is not trusted. Any mismatch exits 12.
5. Decrypt with the kit's protected secret and the passphrase, into a mode `0700` temporary directory that is shredded before the script exits. The passphrase is passed to gpg on `--passphrase-fd` through a pipe (`pinentry-mode loopback`). It is not an argument and it is not written to a file.
6. Decide, then act (section 6.1).
7. Unless `--check`: unset `OMG_BACKUP_PASSPHRASE`, then `./omg watchdog ensure` and `./omg doctor`. The passphrase is also removed from the environment of git, gh, gpg, and shred.

`OMG_HOME` and `OMG_CONFIG_DIR` are honored (`paths.py`). `OMG_DISABLE_TRANSPORT=1` is inherited by `./omg` and by the watchdog it starts. `post_json` returns before it opens a socket or builds a URL, key, or header when that variable is set, so a restored `hooks.json` cannot wake a live bot during a drill.

### 6.1 When restore writes

Compare the local database with the snapshot:

| Local database | Result |
|---|---|
| missing | restore |
| `PRAGMA integrity_check` is not `ok`, or the file is not a readable database | restore |
| ordered event stream is a strict prefix of the snapshot (equal leading events, snapshot has more) | restore |
| ordered event stream equals the snapshot | no-op, exit 0 |
| snapshot stream is a strict prefix of the local stream (local is newer and consistent) | no-op, exit 0, local files unchanged |
| anything else (diverged) | exit 11, nothing changed |

Restore replaces the database (SQLite backup API onto a temporary file, then `rename`; stale `-wal` and `-shm` siblings are removed first), replaces `jobs/` and `state/logs/`, writes `config/policy.json` when the snapshot has one, and writes `hooks.json` as mode `0600` inside the `0700` secrets directory when the snapshot has one. Files under `jobs/` keep the owner-execute bit when the archived file had it (`0700`). Group and other permission bits are not restored. Logs, policy, the database, and `hooks.json` stay `0600`. A snapshot that has no `hooks.json` does not delete a local one. The bundle ciphertext, signature, and sidecar manifest are installed into the local bundle directory so the box keeps the snapshot it just trusted.

`--check` performs steps 3–6 in a temporary directory and does not modify `OMG_HOME`, the config directory, or the jobs tree. It does not run bootstrap, watchdog, or doctor.

A second run against an equal, healthy database is a no-op (exit 0). `watchdog ensure` on that second run is the existing idempotent start.

### 6.2 Exit codes

| Code | Meaning |
|---|---|
| 0 | restored, or already current / local newer (nothing changed), or `--check` and healthy and current |
| 1 | internal error caught by `python3 -m oh_my_grok.recover`; a traceback is not printed |
| 2 | usage, missing `python3` or `gpg`, or `OMG_BACKUP_PASSPHRASE` unset or empty |
| 8 | `scripts/bootstrap.sh` failed |
| 10 | no bundle found |
| 11 | local state has diverged from the snapshot; nothing changed |
| 12 | signature invalid, or manifest hashes do not match the archive; nothing changed |
| 13 | decrypt failed; nothing changed |
| 14 | `--check` only: a restore would be required (missing, corrupt, or a strict prefix) |
| 15 | restore was applied, then `./omg watchdog ensure` or `./omg doctor` failed |

## 7. Disposable drill

`tests/integration/test_durability_drill.py` uses `OMG_HOME` and other temporary directories only. It seeds a database and a `hooks.json`, takes a bundle, wipes the home, and runs `scripts/recover.sh`. It checks SQLite `integrity_check`, the restored manifest hashes, and the `hooks.json` SHA-256. It does not print hook bytes; a leak fails with a fixed sentence that does not contain the secret.

The same test covers a second run (no-op), a newer local stream (no overwrite), a diverged stream (exit 11, no overwrite), a corrupt database (restore), a tampered ciphertext (exit 12), and a bad signature (exit 12). The drill sets `OMG_DISABLE_TRANSPORT=1` and leaves `features.watchdog` and `features.backup` off in the seeded policy so the restored hook file cannot POST and doctor stays on the existing SKIP path.

## 8. Not in this change

- Slice 0: proving the real `/cursor/stores/user` mount accepts a bundle.
- Slice 6: a routine that requests a backup.
- Slice 7: copies of this design in a skill or an offline kit.
- Contacting the public `pysol-dev/oh-my-grok` repository. That target is refused.
- Any edit to `spec/IMPLEMENTATION.md`.
