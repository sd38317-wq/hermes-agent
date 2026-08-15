from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.ops import nas_incremental_index as indexer


class NasIncrementalIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "nas"
        self.source.mkdir()
        self.db = self.root / "private" / "index.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = indexer.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_initial_scan_creates_private_metadata_schema(self):
        photo = self.source / "Trips" / "Beach.JPG"
        photo.parent.mkdir()
        photo.write_bytes(b"not copied into the index")

        code, output, error = self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "photos"
        )

        self.assertEqual((0, ""), (code, error))
        self.assertIn("scan complete", output.lower())
        self.assertEqual(0o700, self.db.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.db.stat().st_mode & 0o777)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            self.assertTrue({
                "share", "relative_path", "filename", "extension", "size",
                "mtime_ns", "file_type", "last_seen", "present", "content_hash",
            } <= columns)
            row = conn.execute(
                "SELECT share,relative_path,filename,extension,size,file_type,present,content_hash "
                "FROM files"
            ).fetchone()
            self.assertEqual(
                ("photos", "Trips/Beach.JPG", "Beach.JPG", ".jpg", len(photo.read_bytes()),
                 "image", 1, None),
                row,
            )
            stored_text = " ".join(
                str(value) for db_row in conn.execute("SELECT * FROM files") for value in db_row
                if value is not None
            )
            self.assertNotIn(str(self.source), stored_text)
            self.assertNotIn("not copied into the index", stored_text)

    def test_rejects_source_or_database_symlink_and_db_inside_source(self):
        source_link = self.root / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)
        db_target = self.root / "real.sqlite3"
        db_target.touch()
        db_link = self.root / "db-link.sqlite3"
        db_link.symlink_to(db_target)

        for source, db in (
            (source_link, self.db),
            (self.source, db_link),
            (self.source, self.source / "index.sqlite3"),
            (
                self.source,
                self.source.parent / "outside" / ".." / self.source.name / "escaped.sqlite3",
            ),
        ):
            with self.subTest(source=source, db=db):
                code, _, error = self.run_cli(
                    "--db", str(db), "scan", str(source), "--share", "photos"
                )
                self.assertEqual(2, code)
                self.assertIn("error", error.lower())

    def test_rejects_path_like_share_labels(self):
        for share in ("../escape", "/mnt/private/share", "line\nbreak"):
            with self.subTest(share=share):
                code, output, error = self.run_cli(
                    "--db", str(self.db), "scan", str(self.source), "--share", share
                )
                self.assertEqual(2, code)
                self.assertEqual("", output)
                self.assertIn("share", error.lower())

    def test_incremental_lifecycle_and_offline_index_remains_searchable(self):
        changing = self.source / "changing.txt"
        deleted = self.source / "deleted.pdf"
        renamed_old = self.source / "old-name.jpg"
        changing.write_text("one", encoding="utf-8")
        deleted.write_bytes(b"gone later")
        renamed_old.write_bytes(b"rename me")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "media"
        )[0])

        changing.write_text("a larger replacement", encoding="utf-8")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "media"
        )[0])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                len("a larger replacement"),
                conn.execute("SELECT size FROM files WHERE filename=?", (changing.name,)).fetchone()[0],
            )

        deleted.unlink()
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "media"
        )[0])
        renamed_new = self.source / "new-name.jpg"
        renamed_old.rename(renamed_new)
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "media"
        )[0])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            states = dict(conn.execute("SELECT filename,present FROM files"))
        self.assertEqual(0, states["deleted.pdf"])
        self.assertEqual(0, states["old-name.jpg"])
        self.assertEqual(1, states["new-name.jpg"])

        last_success_before = None
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            last_success_before = conn.execute(
                "SELECT last_success FROM shares WHERE share=?", ("media",)
            ).fetchone()[0]
        unavailable = self.root / "nas-offline"
        self.source.rename(unavailable)
        code, output, error = self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "media"
        )
        self.assertEqual(3, code)
        self.assertIn("offline", (output + error).lower())
        code, output, error = self.run_cli(
            "--db", str(self.db), "search", "new-name", "--share", "media"
        )
        self.assertEqual((0, ""), (code, error))
        self.assertIn("OFFLINE", output)
        self.assertIn("new-name.jpg", output)
        self.assertNotIn(str(unavailable), output)
        code, output, error = self.run_cli("--db", str(self.db), "status", "--share", "media")
        self.assertEqual((0, ""), (code, error))
        self.assertIn("OFFLINE", output)
        self.assertIn("last successful update", output.lower())
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (last_success_before, 1),
                conn.execute(
                    "SELECT last_success,present FROM shares JOIN files USING(share) "
                    "WHERE share=? AND filename=?", ("media", "new-name.jpg")
                ).fetchone(),
            )

    def test_hashes_only_same_size_and_extension_duplicate_candidates(self):
        duplicate_a = self.source / "a.bin"
        duplicate_b = self.source / "nested" / "b.BIN"
        unique = self.source / "unique.bin"
        duplicate_b.parent.mkdir()
        duplicate_a.write_bytes(b"same bytes")
        duplicate_b.write_bytes(b"same bytes")
        unique.write_bytes(b"unique size")
        opened: list[str] = []
        real_hash = indexer._hash_file_at

        def recording_hash(directory_fd, name, size, mtime_ns):
            opened.append(name)
            if name == unique.name:
                raise AssertionError("noncandidate content was opened")
            return real_hash(directory_fd, name, size, mtime_ns)

        with mock.patch.object(indexer, "_hash_file_at", side_effect=recording_hash):
            code, _, error = self.run_cli(
                "--db", str(self.db), "scan", str(self.source), "--share", "dupes"
            )
        self.assertEqual((0, ""), (code, error))
        self.assertEqual({duplicate_a.name, duplicate_b.name}, set(opened))
        expected = hashlib.sha256(b"same bytes").hexdigest()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            hashes = dict(conn.execute("SELECT filename,content_hash FROM files"))
        self.assertEqual(expected, hashes["a.bin"])
        self.assertEqual(expected, hashes["b.BIN"])
        self.assertIsNone(hashes["unique.bin"])

    def test_search_filters_name_path_extension_and_type(self):
        fixtures = {
            "Trips/Beach.JPG": b"photo",
            "Work/Beach-notes.txt": b"notes are longer",
            "Trips/movie.mp4": b"video is longest here",
        }
        for relative, content in fixtures.items():
            path = self.source / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content)
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "library"
        )[0])

        cases = (
            (("--name", "beach"), {"Trips/Beach.JPG", "Work/Beach-notes.txt"}),
            (("--path", "Trips"), {"Trips/Beach.JPG", "Trips/movie.mp4"}),
            (("--extension", "JPG"), {"Trips/Beach.JPG"}),
            (("--type", "video"), {"Trips/movie.mp4"}),
        )
        for options, expected in cases:
            with self.subTest(options=options):
                code, output, error = self.run_cli(
                    "--db", str(self.db), "search", "--share", "library", *options
                )
                self.assertEqual((0, ""), (code, error))
                found = {path for path in fixtures if path in output}
                self.assertEqual(expected, found)

    def test_name_and_path_substrings_use_trigram_virtual_index(self):
        target = self.source / "Photographs" / "vacation-gallery.jpg"
        target.parent.mkdir()
        target.write_bytes(b"image metadata")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "fast"
        )[0])

        code, output, error = self.run_cli(
            "--db", str(self.db), "search", "togra", "--share", "fast"
        )
        self.assertEqual((0, ""), (code, error))
        self.assertIn("Photographs/vacation-gallery.jpg", output)
        for options in (("--name", "ation"), ("--path", "togra")):
            with self.subTest(options=options):
                code, output, error = self.run_cli(
                    "--db", str(self.db), "search", "--share", "fast", *options
                )
                self.assertEqual((0, ""), (code, error))
                self.assertIn("Photographs/vacation-gallery.jpg", output)
                sql, params = indexer._build_search_query(
                    "", "fast",
                    options[1] if options[0] == "--name" else None,
                    options[1] if options[0] == "--path" else None,
                    None, None,
                )
                with contextlib.closing(sqlite3.connect(self.db)) as conn:
                    plan = " ".join(
                        str(column)
                        for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                        for column in row
                    ).upper()
                self.assertIn("VIRTUAL TABLE INDEX", plan)
                self.assertRegex(plan, r"VIRTUAL TABLE INDEX \d+:[A-Z0-9]+")

    def test_short_text_filter_is_rejected_instead_of_silently_missing_results(self):
        (self.source / "x.jpg").write_bytes(b"metadata")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "short"
        )[0])

        code, output, error = self.run_cli(
            "--db", str(self.db), "search", "--share", "short", "--name", "x"
        )

        self.assertEqual(2, code)
        self.assertNotIn("x.jpg", output)
        self.assertIn("at least 3", error.lower())

    def test_script_runs_directly_and_import_has_no_side_effects(self):
        script = Path(indexer.__file__)
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("scan", result.stdout)
        self.assertFalse(self.db.exists())

    def test_failed_traversal_rolls_back_and_marks_offline_without_deletion(self):
        survivor = self.source / "survivor.txt"
        survivor.write_text("keep indexed", encoding="utf-8")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "fragile"
        )[0])

        def broken_walk(*args, **kwargs):
            yield ".", [], [], -1
            raise PermissionError("mount disconnected")

        with mock.patch.object(indexer.os, "fwalk", side_effect=broken_walk):
            code, _, error = self.run_cli(
                "--db", str(self.db), "scan", str(self.source), "--share", "fragile"
            )
        self.assertEqual(3, code)
        self.assertIn("OFFLINE", error)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (1, 0),
                conn.execute(
                    "SELECT present,online FROM files JOIN shares USING(share) "
                    "WHERE filename=?", (survivor.name,)
                ).fetchone(),
            )

    def test_file_disappearing_during_candidate_hash_does_not_abort_scan(self):
        first = self.source / "first.dat"
        vanishing = self.source / "vanishing.dat"
        first.write_bytes(b"candidate")
        vanishing.write_bytes(b"candidate")
        original_hash = indexer._hash_file_at

        def disappear(directory_fd: int, name: str, size: int, mtime_ns: int):
            if name == vanishing.name:
                vanishing.unlink()
            return original_hash(directory_fd, name, size, mtime_ns)

        with mock.patch.object(indexer, "_hash_file_at", side_effect=disappear):
            code, _, error = self.run_cli(
                "--db", str(self.db), "scan", str(self.source), "--share", "racy"
            )
        self.assertEqual((0, ""), (code, error))
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            rows = dict(conn.execute("SELECT filename,content_hash FROM files"))
        self.assertIsNotNone(rows["first.dat"])
        self.assertIsNone(rows["vanishing.dat"])

    def test_file_io_failure_aborts_scan_without_changing_prior_state(self):
        protected = self.source / "protected.txt"
        protected.write_text("indexed before failure", encoding="utf-8")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "io-failure"
        )[0])
        real_stat = indexer.os.stat

        def denied(path, *args, **kwargs):
            if path == protected.name and kwargs.get("dir_fd") is not None:
                raise PermissionError("simulated read failure")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(indexer.os, "stat", side_effect=denied):
            code, _, error = self.run_cli(
                "--db", str(self.db), "scan", str(self.source), "--share", "io-failure"
            )
        self.assertEqual(3, code)
        self.assertIn("OFFLINE", error)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (1, 0),
                conn.execute(
                    "SELECT present,online FROM files JOIN shares USING(share) "
                    "WHERE filename=?", (protected.name,)
                ).fetchone(),
            )

    def test_operator_documentation_covers_safe_usage(self):
        document = Path("scripts/ops/NAS_INCREMENTAL_INDEX.md").read_text(encoding="utf-8")
        for required in (
            "/opt/data/scripts/", "/opt/data/cache/nas-index/nas-index.sqlite3",
            "read-only", "SSHFS", "SMB", "scan", "search", "status",
            "offline", "last successful", "credentials",
        ):
            with self.subTest(required=required):
                self.assertIn(required.lower(), document.lower())

    def test_unexpected_empty_mount_preserves_rows_until_explicitly_allowed(self):
        indexed = self.source / "only-file.txt"
        indexed.write_text("keep this metadata", encoding="utf-8")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "mount"
        )[0])
        indexed.unlink()

        code, _, error = self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "mount"
        )
        self.assertEqual(3, code)
        self.assertIn("OFFLINE", error)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (1, 0),
                conn.execute(
                    "SELECT present,online FROM files JOIN shares USING(share) "
                    "WHERE filename=?", (indexed.name,)
                ).fetchone(),
            )

        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "mount",
            "--allow-empty",
        )[0])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(0, conn.execute(
                "SELECT present FROM files WHERE filename=?", (indexed.name,)
            ).fetchone()[0])

    def test_search_escapes_terminal_control_characters_in_paths(self):
        unsafe = self.source / "line\nbreak-\x1b[31m.txt"
        unsafe.write_text("metadata only", encoding="utf-8")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "safe-output"
        )[0])

        code, output, error = self.run_cli(
            "--db", str(self.db), "search", "line", "--share", "safe-output"
        )
        self.assertEqual((0, ""), (code, error))
        self.assertNotIn("\x1b", output)
        self.assertNotIn("line\nbreak", output)
        self.assertIn(r"line\nbreak-\x1b[31m.txt", output)

    def test_rejects_world_writable_database_parent(self):
        self.db.parent.mkdir(mode=0o700)
        self.db.parent.chmod(0o777)

        code, _, error = self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "unsafe-parent"
        )

        self.assertEqual(2, code)
        self.assertIn("private", error.lower())

    def test_rejects_symlink_lock_without_touching_target(self):
        self.db.parent.mkdir(mode=0o700)
        target = self.root / "lock-target"
        target.write_text("unchanged", encoding="utf-8")
        self.db.with_suffix(self.db.suffix + ".lock").symlink_to(target)

        code, _, error = self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "lock"
        )

        self.assertEqual(2, code)
        self.assertIn("lock", error.lower())
        self.assertEqual("unchanged", target.read_text(encoding="utf-8"))

    def test_stale_offline_attempt_cannot_override_newer_success(self):
        self.db.parent.mkdir(mode=0o700)
        with contextlib.closing(indexer._prepare_db(self.db)) as conn:
            conn.execute(
                "INSERT INTO shares(share,online,last_attempt,last_success,last_error) "
                "VALUES ('race',1,200,200,NULL)"
            )
            conn.commit()

        indexer._mark_offline(self.db, "race", attempted=100)

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (1, 200, 200, None),
                conn.execute(
                    "SELECT online,last_attempt,last_success,last_error FROM shares "
                    "WHERE share='race'"
                ).fetchone(),
            )

    def test_stale_success_attempt_cannot_override_newer_offline_state(self):
        self.db.parent.mkdir(mode=0o700)
        (self.source / "old-snapshot.txt").write_text("metadata", encoding="utf-8")
        with contextlib.closing(indexer._prepare_db(self.db)) as conn:
            conn.execute(
                "INSERT INTO shares(share,online,last_attempt,last_success,last_error) "
                "VALUES ('race-success',0,200,150,'source unavailable')"
            )
            conn.commit()

        self.assertIsNone(
            indexer._scan_online(
                str(self.source), self.db, "race-success", attempted=100,
                allow_empty=False,
            )
        )

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (0, 200, 150, "source unavailable", 0),
                (
                    *conn.execute(
                        "SELECT online,last_attempt,last_success,last_error FROM shares "
                        "WHERE share='race-success'"
                    ).fetchone(),
                    conn.execute(
                        "SELECT COUNT(*) FROM files WHERE share='race-success'"
                    ).fetchone()[0],
                ),
            )

    def test_open_directory_fd_remains_anchored_after_path_replacement(self):
        original = self.source / "original.txt"
        original.write_text("old tree", encoding="utf-8")

        fd = indexer._open_directory(self.source)
        try:
            moved = self.root / "moved-source"
            self.source.rename(moved)
            self.source.mkdir()
            (self.source / "replacement.txt").write_text("new tree", encoding="utf-8")

            self.assertEqual(
                len("old tree"),
                os.stat("original.txt", dir_fd=fd, follow_symlinks=False).st_size,
            )
            with self.assertRaises(FileNotFoundError):
                os.stat("replacement.txt", dir_fd=fd, follow_symlinks=False)
        finally:
            os.close(fd)

    def test_database_connection_fails_closed_after_parent_replacement(self):
        self.db.parent.mkdir(mode=0o700)
        conn = indexer._prepare_db(self.db)
        try:
            moved = self.root / "moved-private"
            self.db.parent.rename(moved)
            self.db.parent.mkdir(mode=0o700)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO shares(share,online,last_attempt,last_success,last_error) "
                    "VALUES ('anchored',1,1,1,NULL)"
                )
        finally:
            conn.close()

        self.assertFalse(self.db.exists())
        with contextlib.closing(sqlite3.connect(moved / self.db.name)) as check:
            self.assertIsNone(
                check.execute("SELECT online FROM shares WHERE share='anchored'").fetchone()
            )

    def test_stdout_failure_after_commit_does_not_mark_share_offline(self):
        (self.source / "indexed.txt").write_text("metadata", encoding="utf-8")

        with mock.patch("builtins.print", side_effect=[OSError("closed pipe"), None]):
            with self.assertRaises(OSError):
                indexer.scan(str(self.source), self.db, "stdout")

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (1, 1),
                conn.execute(
                    "SELECT online,present FROM shares JOIN files USING(share) "
                    "WHERE share='stdout'"
                ).fetchone(),
            )

    def test_candidate_hash_permission_failure_rolls_back_and_marks_offline(self):
        (self.source / "one.dat").write_bytes(b"duplicate")
        (self.source / "two.dat").write_bytes(b"duplicate")
        self.assertEqual(0, self.run_cli(
            "--db", str(self.db), "scan", str(self.source), "--share", "hash-io"
        )[0])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            hashes_before = dict(conn.execute(
                "SELECT filename,content_hash FROM files WHERE share='hash-io'"
            ))

        real_open = indexer.os.open

        def deny_candidate_open(path, flags, *args, **kwargs):
            if path in {"one.dat", "two.dat"} and kwargs.get("dir_fd") is not None:
                raise PermissionError("read denied")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(indexer.os, "open", side_effect=deny_candidate_open):
            code, _, error = self.run_cli(
                "--db", str(self.db), "scan", str(self.source), "--share", "hash-io"
            )

        self.assertEqual(3, code)
        self.assertIn("OFFLINE", error)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                (0, hashes_before),
                (
                    conn.execute("SELECT online FROM shares WHERE share='hash-io'").fetchone()[0],
                    dict(conn.execute(
                        "SELECT filename,content_hash FROM files WHERE share='hash-io'"
                    )),
                ),
            )


if __name__ == "__main__":
    unittest.main()
