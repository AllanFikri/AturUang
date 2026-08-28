import hashlib
from io import BytesIO
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from aturuang.ingestion_discovery import (
    ArchiveSafetyError,
    DiscoveryError,
    DiscoveryPolicy,
    discover_files,
    sha256_file,
)


def zip_bytes(entries):
    buffer = BytesIO()
    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


class TestUniversalIngestionPhase2SafeDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_01_standalone_supported_file_is_discovered(self):
        data = b"%PDF synthetic fixture"
        path = self.root / "statement.pdf"
        path.write_bytes(data)

        result = discover_files(self.root)

        self.assertEqual(result.unique_artifact_count, 1)
        self.assertEqual(result.supported_occurrences, 1)
        artifact = result.artifacts[0]
        self.assertEqual(
            artifact.content_sha256,
            hashlib.sha256(data).hexdigest(),
        )
        self.assertEqual(
            artifact.occurrences[0].source_locator,
            "statement.pdf",
        )

    def test_02_nested_directory_is_discovered_deterministically(self):
        (self.root / "b").mkdir()
        (self.root / "a").mkdir()
        (self.root / "b" / "two.csv").write_bytes(b"two")
        (self.root / "a" / "one.pdf").write_bytes(b"one")

        first = discover_files(self.root)
        second = discover_files(self.root)

        self.assertEqual(first, second)
        locators = sorted(
            occurrence.source_locator
            for artifact in first.artifacts
            for occurrence in artifact.occurrences
        )
        self.assertEqual(
            locators,
            ["a/one.pdf", "b/two.csv"],
        )

    def test_03_unsupported_extension_is_not_an_artifact(self):
        (self.root / "notes.txt").write_text(
            "private-like synthetic content",
            encoding="utf-8",
        )

        result = discover_files(self.root)

        self.assertEqual(result.unique_artifact_count, 0)
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(
            result.diagnostics[0].code,
            "UNSUPPORTED_EXTENSION",
        )

    def test_04_exact_standalone_duplicates_collapse_to_one_artifact(self):
        data = b"same bytes"
        (self.root / "a.pdf").write_bytes(data)
        (self.root / "b.pdf").write_bytes(data)

        result = discover_files(self.root)

        self.assertEqual(result.unique_artifact_count, 1)
        self.assertEqual(result.supported_occurrences, 2)
        self.assertEqual(
            len(result.artifacts[0].occurrences),
            2,
        )

    def test_05_standalone_and_zip_copy_deduplicate(self):
        data = b"same statement"
        (self.root / "statement.pdf").write_bytes(data)
        (self.root / "archive.zip").write_bytes(
            zip_bytes([("nested/statement.pdf", data)])
        )

        result = discover_files(self.root)

        self.assertEqual(result.unique_artifact_count, 1)
        self.assertEqual(result.supported_occurrences, 2)
        locators = {
            item.source_locator
            for item in result.artifacts[0].occurrences
        }
        self.assertEqual(
            locators,
            {
                "statement.pdf",
                "archive.zip!/nested/statement.pdf",
            },
        )

    def test_06_nested_zip_copy_deduplicates(self):
        data = b"nested statement"
        inner = zip_bytes([("statement.pdf", data)])
        outer = zip_bytes(
            [
                ("inner.zip", inner),
                ("copy.pdf", data),
            ]
        )
        (self.root / "outer.zip").write_bytes(outer)

        result = discover_files(self.root)

        self.assertEqual(result.unique_artifact_count, 1)
        self.assertEqual(result.supported_occurrences, 2)
        self.assertEqual(result.archives_seen, 2)

    def test_07_archive_lineage_preserves_each_archive_hop(self):
        data = b"deep document"
        inner = zip_bytes([("folder/doc.pdf", data)])
        outer = zip_bytes([("inner.zip", inner)])
        (self.root / "outer.zip").write_bytes(outer)

        result = discover_files(self.root)

        occurrence = result.artifacts[0].occurrences[0]
        self.assertEqual(len(occurrence.archive_lineage), 2)
        self.assertEqual(
            occurrence.archive_lineage[0].member_path,
            "inner.zip",
        )
        self.assertEqual(
            occurrence.archive_lineage[1].member_path,
            "folder/doc.pdf",
        )
        self.assertEqual(
            occurrence.archive_lineage[0].archive_depth,
            1,
        )
        self.assertEqual(
            occurrence.archive_lineage[1].archive_depth,
            2,
        )

    def test_08_zip_slip_dotdot_is_rejected(self):
        (self.root / "bad.zip").write_bytes(
            zip_bytes([("../escape.pdf", b"x")])
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(self.root)

    def test_09_zip_slip_backslash_dotdot_is_rejected(self):
        (self.root / "bad.zip").write_bytes(
            zip_bytes([("..\\escape.pdf", b"x")])
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(self.root)

    def test_10_absolute_archive_member_is_rejected(self):
        (self.root / "bad.zip").write_bytes(
            zip_bytes([("/absolute.pdf", b"x")])
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(self.root)

    def test_11_drive_prefixed_archive_member_is_rejected(self):
        (self.root / "bad.zip").write_bytes(
            zip_bytes([("C:/escape.pdf", b"x")])
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(self.root)

    def test_12_archive_recursion_depth_fails_closed(self):
        level3 = zip_bytes([("doc.pdf", b"x")])
        level2 = zip_bytes([("l3.zip", level3)])
        level1 = zip_bytes([("l2.zip", level2)])
        (self.root / "l1.zip").write_bytes(level1)

        policy = DiscoveryPolicy(max_archive_depth=2)

        with self.assertRaises(ArchiveSafetyError):
            discover_files(
                self.root,
                policy=policy,
            )

    def test_13_file_count_limit_fails_closed(self):
        (self.root / "many.zip").write_bytes(
            zip_bytes(
                [
                    ("1.pdf", b"1"),
                    ("2.pdf", b"2"),
                    ("3.pdf", b"3"),
                ]
            )
        )

        policy = DiscoveryPolicy(max_files=3)

        with self.assertRaises(ArchiveSafetyError):
            discover_files(
                self.root,
                policy=policy,
            )

    def test_14_single_file_size_limit_fails_closed(self):
        (self.root / "large.pdf").write_bytes(b"x" * 11)

        policy = DiscoveryPolicy(
            max_single_file_bytes=10,
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(
                self.root,
                policy=policy,
            )

    def test_15_total_uncompressed_size_limit_fails_closed(self):
        (self.root / "a.pdf").write_bytes(b"a" * 6)
        (self.root / "b.pdf").write_bytes(b"b" * 6)

        policy = DiscoveryPolicy(
            max_total_uncompressed_bytes=10,
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(
                self.root,
                policy=policy,
            )

    def test_16_compression_ratio_limit_fails_closed(self):
        payload = b"A" * 10000
        (self.root / "bombish.zip").write_bytes(
            zip_bytes([("very-compressible.pdf", payload)])
        )

        policy = DiscoveryPolicy(
            max_compression_ratio=2.0,
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(
                self.root,
                policy=policy,
            )

    def test_17_corrupt_zip_fails_closed(self):
        (self.root / "corrupt.zip").write_bytes(
            b"not a real zip"
        )

        with self.assertRaises(ArchiveSafetyError):
            discover_files(self.root)

    def test_18_sha256_file_is_streamed_and_correct(self):
        data = (b"0123456789" * 200000)
        path = self.root / "large.pdf"
        path.write_bytes(data)

        self.assertEqual(
            sha256_file(path),
            hashlib.sha256(data).hexdigest(),
        )

    def test_19_diagnostic_does_not_include_file_content(self):
        secret = "SUPER-SECRET-SYNTHETIC-PAYLOAD"
        path = self.root / "private.txt"
        path.write_text(
            secret,
            encoding="utf-8",
        )

        result = discover_files(self.root)
        rendered = repr(result)

        self.assertNotIn(secret, rendered)
        self.assertNotIn(
            str(path),
            rendered,
        )

    def test_20_error_uses_locator_token_not_absolute_path(self):
        path = self.root / "oversize.pdf"
        path.write_bytes(b"x" * 11)

        policy = DiscoveryPolicy(
            max_single_file_bytes=10,
        )

        try:
            discover_files(
                self.root,
                policy=policy,
            )
        except ArchiveSafetyError as exc:
            message = str(exc)
        else:
            self.fail("expected ArchiveSafetyError")

        self.assertNotIn(
            str(self.root),
            message,
        )
        self.assertIn("token=", message)

    def test_21_single_file_root_is_supported(self):
        path = self.root / "one.pdf"
        path.write_bytes(b"one")

        result = discover_files(path)

        self.assertEqual(result.unique_artifact_count, 1)
        self.assertEqual(
            result.artifacts[0].occurrences[0].source_locator,
            "one.pdf",
        )

    def test_22_missing_root_fails_closed(self):
        with self.assertRaises(DiscoveryError):
            discover_files(
                self.root / "does-not-exist",
            )

    def test_23_policy_normalizes_extensions(self):
        policy = DiscoveryPolicy(
            allowed_extensions=("PDF", ".JPG", "csv"),
            archive_extensions=("ZIP",),
        )

        self.assertEqual(
            policy.allowed_extensions,
            (".csv", ".jpg", ".pdf"),
        )
        self.assertEqual(
            policy.archive_extensions,
            (".zip",),
        )

    def test_24_zero_byte_supported_file_is_discovered_for_later_preflight(self):
        (self.root / "empty.pdf").write_bytes(b"")

        result = discover_files(self.root)

        self.assertEqual(result.unique_artifact_count, 1)
        self.assertEqual(
            result.artifacts[0].size_bytes,
            0,
        )

    def test_25_same_bytes_different_extensions_preserve_occurrence_extensions(self):
        data = b"same bytes with conflicting observed extensions"
        (self.root / "a.pdf").write_bytes(data)
        (self.root / "b.jpg").write_bytes(data)

        result = discover_files(self.root)

        self.assertEqual(result.unique_artifact_count, 1)
        artifact = result.artifacts[0]
        self.assertEqual(artifact.extension, "")
        self.assertEqual(artifact.observed_extensions, (".jpg", ".pdf"))
        by_locator = {
            occurrence.source_locator: occurrence.extension
            for occurrence in artifact.occurrences
        }
        self.assertEqual(by_locator, {"a.pdf": ".pdf", "b.jpg": ".jpg"})

    def test_26_duplicate_normalized_zip_member_path_fails_closed(self):
        buffer = BytesIO()
        with zipfile.ZipFile(
            buffer,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr("folder/doc.pdf", b"one")
            archive.writestr("folder\\doc.pdf", b"two")

        (self.root / "duplicate-path.zip").write_bytes(buffer.getvalue())

        with self.assertRaises(ArchiveSafetyError):
            discover_files(self.root)

    def test_27_zip_symlink_member_fails_closed(self):
        buffer = BytesIO()
        with zipfile.ZipFile(
            buffer,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            info = zipfile.ZipInfo("linked.pdf")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"target.pdf")

        (self.root / "symlink.zip").write_bytes(buffer.getvalue())

        with self.assertRaises(ArchiveSafetyError):
            discover_files(self.root)

if __name__ == "__main__":
    unittest.main()
