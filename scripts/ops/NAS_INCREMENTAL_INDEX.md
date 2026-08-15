# NAS metadata index

`nas_incremental_index.py` builds a local, searchable metadata inventory. It
does not copy file contents or photos. Content is opened only when two or more
present files have the same size and normalized extension, in order to compute
a streaming SHA-256 duplicate check.

## Operator prerequisite

Mount the NAS share before scanning. Mounting is deliberately outside this
script: use the site's approved NFS, SSHFS, or SMB tooling and configuration.
The mount must be read-only (for example, the platform's `ro` mount option),
and the account used for the mount should have read-only permissions. Confirm
the mount and permissions using the operating system's normal administrative
procedure before indexing. The script never mounts a share or invokes a shell.

For deployment, install the script at
`/opt/data/scripts/nas_incremental_index.py`. Keep the SQLite database on a
local filesystem, not on the NAS. Its default location is
`/opt/data/cache/nas-index/nas-index.sqlite3`.

## Usage

Run a scan and give the mounted namespace a stable, non-secret share name:

```bash
python3 /opt/data/scripts/nas_incremental_index.py scan /mnt/nas/photos --share photos
```

Use `--db /local/path/index.sqlite3` before the command to override the local
database. The database must not be inside the source and neither path may be a
symbolic link.

Search present files by general text or focused filters:

```bash
python3 /opt/data/scripts/nas_incremental_index.py search beach --share photos
python3 /opt/data/scripts/nas_incremental_index.py search --share photos --extension jpg
python3 /opt/data/scripts/nas_incremental_index.py search --path Trips --type image
python3 /opt/data/scripts/nas_incremental_index.py status --share photos
```

General text, `--name`, and `--path` substring terms must contain at least three
characters so SQLite can use the trigram index instead of silently returning
incomplete short-term results. Extension and type filters have no such minimum.

`scan` exits 0 on success, 3 when the source is offline/unavailable, and 2 for
invalid input or a database error. `search` and `status` work while a source is
offline. Their output clearly reports `OFFLINE` and the last successful update.
An unsuccessful or interrupted traversal preserves all prior present/deleted
state; rows are marked deleted only after an entire successful traversal.
Renames appear as a deleted old relative path plus a present new path.

As a fail-closed mount-loss guard, a scan that unexpectedly finds zero files
while the share still has present rows is treated as `OFFLINE` and preserves
the prior index. After verifying the read-only mount is healthy, use
`scan ... --allow-empty` only when deleting every remaining file is intentional.

No credentials belong in this script, the database, command arguments, or
logs. Store NAS/SSHFS/SMB credentials only in the operator-approved secret
facility used by the mount service. Use a share label rather than a server
address or credential-bearing mount string.
