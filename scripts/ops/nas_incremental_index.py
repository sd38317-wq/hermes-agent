#!/usr/bin/env python3
"""Read-only filesystem metadata index backed by a local SQLite database."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Iterator

DEFAULT_DB = Path("/opt/data/cache/nas-index/nas-index.sqlite3")
SHARE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
CURRENT_UID = os.geteuid()  # windows-footgun: ok - POSIX-only NAS operations script

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    share TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    file_type TEXT NOT NULL,
    last_seen INTEGER NOT NULL,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    content_hash TEXT,
    PRIMARY KEY (share, relative_path)
);
CREATE INDEX IF NOT EXISTS files_name_idx ON files(filename COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS files_extension_idx ON files(extension, present);
CREATE INDEX IF NOT EXISTS files_type_idx ON files(file_type, present);
CREATE INDEX IF NOT EXISTS files_path_idx ON files(relative_path COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS shares (
    share TEXT PRIMARY KEY,
    online INTEGER NOT NULL DEFAULT 0 CHECK (online IN (0, 1)),
    last_attempt INTEGER,
    last_success INTEGER,
    last_error TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS file_search USING fts5(
    share UNINDEXED,
    relative_path,
    filename,
    tokenize='trigram'
);
"""

TYPE_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".heic"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"},
    "document": {".pdf", ".txt", ".md", ".doc", ".docx", ".xls", ".xlsx", ".csv"},
    "archive": {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z"},
}


def _reject_symlink(path: Path, label: str) -> None:
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise ValueError(f"{label} must not be a symbolic link")
        if current == current.parent:
            return
        current = current.parent


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory chain without following any symlink component."""
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in absolute.parts[1:]:
            try:
                next_fd = os.open(part, nofollow_flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, nofollow_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def _closing_fd(fd: int) -> Iterator[int]:
    try:
        yield fd
    finally:
        os.close(fd)


def _validate_paths(source: Path, db: Path) -> tuple[Path, Path]:
    _reject_symlink(source, "source")
    _reject_symlink(db, "database")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("source is not a directory")
    db_resolved = Path(os.path.abspath(db))
    try:
        db_resolved.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("database must not be located inside source")
    return source, db_resolved


def _ensure_private_db_parent(path: Path) -> None:
    fd = _open_directory(path, create=True)
    try:
        parent_stat = os.fstat(fd)
        if parent_stat.st_uid != CURRENT_UID or stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise ValueError("database parent must be private (owned by this user and mode 0700)")
    finally:
        os.close(fd)


class _AnchoredConnection(sqlite3.Connection):
    """SQLite connection that keeps its private parent directory descriptor alive."""

    _parent_fd: int | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._parent_fd is not None:
                os.close(self._parent_fd)
                self._parent_fd = None


def _connect_anchored(path: Path, *, create: bool) -> _AnchoredConnection:
    parent_fd = _open_directory(path.parent, create=create)
    try:
        parent_stat = os.fstat(parent_fd)
        if parent_stat.st_uid != CURRENT_UID or stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise ValueError("database parent must be private (owned by this user and mode 0700)")
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT
        db_fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        try:
            db_stat = os.fstat(db_fd)
            if not stat.S_ISREG(db_stat.st_mode) or db_stat.st_uid != CURRENT_UID:
                raise ValueError("database must be a regular file owned by this user")
            os.fchmod(db_fd, 0o600)
        finally:
            os.close(db_fd)
        connection = sqlite3.connect(
            f"/proc/self/fd/{parent_fd}/{path.name}", timeout=10,
            factory=_AnchoredConnection,
        )
        connection._parent_fd = parent_fd
        return connection
    except BaseException:
        os.close(parent_fd)
        raise


def _prepare_db(path: Path) -> sqlite3.Connection:
    conn = _connect_anchored(path, create=True)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


@contextlib.contextmanager
def _scan_lock(db: Path) -> Iterator[None]:
    lock_path = db.with_suffix(db.suffix + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = _open_directory(lock_path.parent)
    try:
        fd = os.open(lock_path.name, flags, 0o600, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        raise ValueError("scan lock must be a regular non-symlink file") from exc
    try:
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != CURRENT_UID:
            raise ValueError("scan lock must be a regular file owned by this user")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise ValueError("another scan is already running") from exc
    finally:
        os.close(fd)
        os.close(parent_fd)


def _extension(name: str) -> str:
    return Path(name).suffix.lower()


def _file_type(extension: str) -> str:
    return next((kind for kind, values in TYPE_EXTENSIONS.items() if extension in values), "other")


def _relative_path(source: Path, path: Path) -> str:
    relative = path.relative_to(source)
    value = PurePosixPath(*relative.parts).as_posix()
    if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise ValueError("unsafe relative path")
    return value


def _hash_file_at(
    directory_fd: int, name: str, expected_size: int, expected_mtime_ns: int
) -> str | None:
    """Hash a stable regular file with constant memory and no symlink following."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            if before.st_size != expected_size or before.st_mtime_ns != expected_mtime_ns:
                return None
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns) != (expected_size, expected_mtime_ns):
                return None
            return digest.hexdigest()
    except FileNotFoundError:
        return None


def _hash_duplicate_candidates(
    conn: sqlite3.Connection, source_fd: int, share: str, seen_at: int
) -> None:
    conn.execute("UPDATE files SET content_hash=NULL WHERE share=? AND last_seen=?", (share, seen_at))
    rows = conn.execute(
        "SELECT relative_path,size,mtime_ns FROM files WHERE share=? AND last_seen=? "
        "AND (size,extension) IN ("
        "SELECT size,extension FROM files WHERE share=? AND last_seen=? "
        "GROUP BY size,extension HAVING COUNT(*) > 1)",
        (share, seen_at, share, seen_at),
    ).fetchall()
    candidates = {relative_path: (size, mtime_ns) for relative_path, size, mtime_ns in rows}
    if not candidates:
        return

    def traversal_error(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames, directory_fd in os.fwalk(
        ".", follow_symlinks=False, onerror=traversal_error, dir_fd=source_fd
    ):
        directory_prefix = PurePosixPath(directory)
        for name in filenames:
            relative_path = (directory_prefix / name).as_posix().removeprefix("./")
            metadata = candidates.get(relative_path)
            if metadata is None:
                continue
            digest = _hash_file_at(directory_fd, name, *metadata)
            if digest is not None:
                conn.execute(
                    "UPDATE files SET content_hash=? WHERE share=? AND relative_path=? AND last_seen=?",
                    (digest, share, relative_path, seen_at),
                )


def _mark_offline(db: Path, share: str, attempted: int) -> None:
    """Record source reachability without changing any indexed file state."""
    try:
        conn = _connect_anchored(db, create=False)
    except FileNotFoundError:
        return
    with contextlib.closing(conn):
        conn.execute(
            "INSERT INTO shares(share,online,last_attempt,last_success,last_error) "
            "VALUES (?,0,?,NULL,'source unavailable') "
            "ON CONFLICT(share) DO UPDATE SET online=0,last_attempt=excluded.last_attempt,"
            "last_error=excluded.last_error "
            "WHERE shares.last_attempt IS NULL OR shares.last_attempt <= excluded.last_attempt",
            (share, attempted),
        )
        conn.commit()


def _scan_online(
    source_arg: str, db: Path, share: str, attempted: int, allow_empty: bool
) -> int | None:
    source, db = _validate_paths(Path(source_arg), db)
    _ensure_private_db_parent(db.parent)
    seen_at = time.time_ns()
    source_fd = _open_directory(source)
    with _closing_fd(source_fd), _scan_lock(db), contextlib.closing(_prepare_db(db)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior_attempt = conn.execute(
            "SELECT last_attempt FROM shares WHERE share=?", (share,)
        ).fetchone()
        if prior_attempt is not None and prior_attempt[0] is not None \
                and int(prior_attempt[0]) > attempted:
            conn.rollback()
            return None
        existing_present = conn.execute(
            "SELECT COUNT(*) FROM files WHERE share=? AND present=1", (share,)
        ).fetchone()[0]
        count = 0
        def traversal_error(error: OSError) -> None:
            raise error

        for directory, dirnames, filenames, directory_fd in os.fwalk(
            ".", follow_symlinks=False, onerror=traversal_error, dir_fd=source_fd
        ):
            for name in filenames:
                try:
                    file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                relative = (PurePosixPath(directory) / name).as_posix().removeprefix("./")
                if not relative or ".." in PurePosixPath(relative).parts:
                    raise ValueError("unsafe relative path")
                extension = _extension(name)
                conn.execute(
                    "INSERT INTO files "
                    "(share,relative_path,filename,extension,size,mtime_ns,file_type,last_seen,present,content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,1,NULL) "
                    "ON CONFLICT(share,relative_path) DO UPDATE SET "
                    "filename=excluded.filename,extension=excluded.extension,size=excluded.size,"
                    "mtime_ns=excluded.mtime_ns,file_type=excluded.file_type,last_seen=excluded.last_seen,"
                    "present=1,content_hash=CASE WHEN files.size=excluded.size AND files.mtime_ns=excluded.mtime_ns "
                    "THEN files.content_hash ELSE NULL END",
                    (share, relative, name, extension, file_stat.st_size, file_stat.st_mtime_ns,
                     _file_type(extension), seen_at),
                )
                count += 1
        if count == 0 and existing_present and not allow_empty:
            raise OSError("source unexpectedly empty")
        _hash_duplicate_candidates(conn, source_fd, share, seen_at)
        conn.execute(
            "UPDATE files SET present=0,content_hash=NULL WHERE share=? AND last_seen<>?",
            (share, seen_at),
        )
        conn.execute("DELETE FROM file_search WHERE share=?", (share,))
        conn.execute(
            "INSERT INTO file_search(share,relative_path,filename) "
            "SELECT share,relative_path,filename FROM files WHERE share=? AND present=1",
            (share,),
        )
        conn.execute(
            "INSERT INTO shares(share,online,last_attempt,last_success,last_error) VALUES (?,1,?,?,NULL) "
            "ON CONFLICT(share) DO UPDATE SET online=1,last_attempt=excluded.last_attempt,"
            "last_success=excluded.last_success,last_error=NULL "
            "WHERE shares.last_attempt IS NULL OR shares.last_attempt <= excluded.last_attempt",
            (share, attempted, seen_at),
        )
        conn.commit()
    return count


def scan(source_arg: str, db: Path, share: str, allow_empty: bool = False) -> int:
    attempted = time.time_ns()
    try:
        count = _scan_online(source_arg, db, share, attempted, allow_empty)
    except OSError:
        _mark_offline(db.absolute(), share, attempted)
        print(f"OFFLINE: share={share}; existing index preserved", file=sys.stderr)
        return 3
    if count is None:
        print(f"Scan superseded: share={share}; newer attempt retained")
        return 0
    print(f"Scan complete: share={share} files={count}")
    return 0


def _open_existing_db(db: Path) -> sqlite3.Connection:
    try:
        conn = _connect_anchored(db.absolute(), create=False)
    except FileNotFoundError:
        raise ValueError("database does not exist; run scan first")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _share_state(conn: sqlite3.Connection, share: str | None) -> list[sqlite3.Row]:
    if share is None:
        return conn.execute("SELECT * FROM shares ORDER BY share").fetchall()
    return conn.execute("SELECT * FROM shares WHERE share=?", (share,)).fetchall()


def _format_time(value: int | None) -> str:
    if value is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value / 1_000_000_000))


def _print_state(rows: list[sqlite3.Row]) -> None:
    for row in rows:
        state = "ONLINE" if row["online"] else "OFFLINE"
        print(f"{row['share']}: {state}; last successful update: {_format_time(row['last_success'])}")


def _fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _display(value: object) -> str:
    """Render untrusted metadata without terminal control characters."""
    return str(value).encode("unicode_escape", errors="backslashreplace").decode("ascii")


def _build_search_query(
    query: str,
    share: str | None,
    name: str | None,
    path: str | None,
    extension: str | None,
    file_type: str | None,
) -> tuple[str, list[object]]:
    for label, value in (("query", query), ("name", name), ("path", path)):
        if value is not None and value != "" and len(value) < 3:
            raise ValueError(f"{label} text must contain at least 3 characters")
        if label != "query" and value == "":
            raise ValueError(f"{label} text must contain at least 3 characters")
    text_terms = []
    if query:
        text_terms.append(_fts_phrase(query))
    if name is not None:
        text_terms.append("filename : " + _fts_phrase(name))
    if path is not None:
        text_terms.append("relative_path : " + _fts_phrase(path))
    from_clause = "files f"
    if text_terms:
        from_clause += (
            " JOIN file_search ON file_search.share=f.share "
            "AND file_search.relative_path=f.relative_path"
        )
    clauses = ["f.present=1"]
    params: list[object] = []
    if text_terms:
        clauses.append("file_search MATCH ?")
        params.append(" AND ".join(text_terms))
    if share is not None:
        clauses.append("f.share=?")
        params.append(share)
    if extension is not None:
        clauses.append("f.extension=?")
        params.append("." + extension.lower().lstrip("."))
    if file_type is not None:
        clauses.append("f.file_type=?")
        params.append(file_type)
    sql = (
        "SELECT f.share,f.relative_path,f.extension,f.file_type,f.size FROM "
        + from_clause + " WHERE " + " AND ".join(clauses)
        + " ORDER BY f.share,f.relative_path LIMIT 1000"
    )
    return sql, params


def search(
    db: Path,
    query: str,
    share: str | None,
    name: str | None = None,
    path: str | None = None,
    extension: str | None = None,
    file_type: str | None = None,
) -> int:
    with contextlib.closing(_open_existing_db(db)) as conn:
        _print_state(_share_state(conn, share))
        sql, params = _build_search_query(
            query, share, name, path, extension, file_type
        )
        rows = conn.execute(sql, params)
        for row in rows:
            print(
                f"{_display(row['share'])}\t{_display(row['relative_path'])}\t"
                f"{_display(row['extension'])}\t{_display(row['file_type'])}\t{row['size']}"
            )
    return 0


def status(db: Path, share: str | None) -> int:
    with contextlib.closing(_open_existing_db(db)) as conn:
        rows = _share_state(conn, share)
        if not rows:
            print("No indexed shares")
        _print_state(rows)
        for row in rows:
            counts = conn.execute(
                "SELECT SUM(present=1),SUM(present=0) FROM files WHERE share=?", (row["share"],)
            ).fetchone()
            print(f"  present={counts[0] or 0} deleted={counts[1] or 0}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="scan filesystem metadata")
    scan_parser.add_argument("source")
    scan_parser.add_argument("--share", default="default")
    scan_parser.add_argument(
        "--allow-empty", action="store_true",
        help="confirm that an empty source should mark all prior files deleted",
    )
    search_parser = subparsers.add_parser("search", help="search indexed metadata")
    search_parser.add_argument("query", nargs="?", default="")
    search_parser.add_argument("--share")
    search_parser.add_argument("--name", help="filename substring")
    search_parser.add_argument("--path", help="relative-path substring")
    search_parser.add_argument("--extension", help="normalized extension, with or without dot")
    search_parser.add_argument("--type", dest="file_type", choices=sorted((*TYPE_EXTENSIONS, "other")))
    status_parser = subparsers.add_parser("status", help="show index status")
    status_parser.add_argument("--share")
    return parser


def _validate_share(share: str | None) -> None:
    if share is not None and SHARE_PATTERN.fullmatch(share) is None:
        raise ValueError("share must be 1-64 letters, digits, dots, underscores, or hyphens")


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        _validate_share(getattr(args, "share", None))
        if args.command == "scan":
            return scan(args.source, args.db, args.share, args.allow_empty)
        if args.command == "search":
            return search(
                args.db, args.query, args.share, args.name, args.path,
                args.extension, args.file_type,
            )
        if args.command == "status":
            return status(args.db, args.share)
        return 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
