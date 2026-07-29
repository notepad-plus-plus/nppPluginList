import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests

import validator


def make_plugin(**overrides):
    plugin = {
        "folder-name": "ExamplePlugin",
        "display-name": "Example Plugin",
        "version": "1.2.3",
        "id": "a" * 64,
        "repository": "https://example.com/ExamplePlugin.zip",
        "description": "An example plugin.",
        "author": "Example Author",
        "homepage": "https://example.com/",
    }
    plugin.update(overrides)
    return plugin


def make_catalog(*plugins, arch="64"):
    return {
        "name": "npp-pluginList",
        "version": "1.2.3",
        "arch": arch,
        "npp-plugins": list(plugins or [make_plugin()]),
    }


def make_zip(entries):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for name, content in entries:
            if isinstance(name, zipfile.ZipInfo):
                output.writestr(name, content)
            else:
                output.writestr(name, content)
    archive.seek(0)
    return archive


class ValidatorTests(unittest.TestCase):
    def reporter(self):
        return validator.Reporter(api_url="", stream=io.StringIO())

    def schema_validator(self, reporter):
        result = validator.build_schema_validator(reporter)
        self.assertIsNotNone(result, reporter.errors)
        return result

    def write_catalog(self, directory, catalog):
        path = Path(directory) / "catalog.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        return path

    def test_strict_json_loader_rejects_duplicate_object_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"name": "first", "name": "second"}', encoding="utf-8")
            with self.assertRaisesRegex(
                validator.DuplicateJsonKeyError, "duplicate JSON object key"
            ):
                validator.load_json_file(path)

    def test_invalid_schema_is_reported_without_follow_on_key_errors(self):
        reporter = self.reporter()
        schema_validator = self.schema_validator(reporter)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_catalog(directory, {"name": "npp-pluginList"})
            result = validator.validate_catalog(path, "64", schema_validator, reporter)
        self.assertIsNone(result)
        self.assertTrue(any("required property" in error for error in reporter.errors))

    def test_schema_rejects_unknown_plugin_fields_and_malformed_hashes(self):
        reporter = self.reporter()
        schema_validator = self.schema_validator(reporter)
        plugin = make_plugin(id="not-a-sha256")
        plugin["unexpected"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_catalog(directory, make_catalog(plugin))
            result = validator.validate_catalog(path, "64", schema_validator, reporter)
        self.assertIsNone(result)
        self.assertTrue(
            any("Additional properties" in error for error in reporter.errors)
        )
        self.assertTrue(any("does not match" in error for error in reporter.errors))

    def test_duplicates_are_case_insensitive_and_checked_offline(self):
        reporter = self.reporter()
        schema_validator = self.schema_validator(reporter)
        plugins = [
            make_plugin(),
            make_plugin(
                **{
                    "folder-name": "exampleplugin",
                    "display-name": "example plugin",
                    "id": "b" * 64,
                    "repository": "https://example.org/other.zip",
                }
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_catalog(directory, make_catalog(*plugins))
            result = validator.validate_catalog(
                path, "64", schema_validator, reporter, require_sorted=False
            )
        self.assertIsNone(result)
        self.assertTrue(
            any("display-name: duplicate" in error for error in reporter.errors)
        )
        self.assertTrue(
            any("folder-name: duplicate" in error for error in reporter.errors)
        )

    def test_filename_architecture_mismatch_is_rejected(self):
        reporter = self.reporter()
        schema_validator = self.schema_validator(reporter)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_catalog(directory, make_catalog(arch="32"))
            result = validator.validate_catalog(path, "64", schema_validator, reporter)
        self.assertIsNone(result)
        self.assertTrue(any("expected '64'" in error for error in reporter.errors))

    def test_unsafe_folder_and_private_download_url_are_rejected(self):
        reporter = self.reporter()
        schema_validator = self.schema_validator(reporter)
        catalog = make_catalog(
            make_plugin(
                **{
                    "folder-name": "../escape",
                    "repository": "https://127.0.0.1/plugin.zip",
                }
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_catalog(directory, catalog)
            result = validator.validate_catalog(path, "64", schema_validator, reporter)
        self.assertIsNone(result)
        self.assertTrue(
            any("unsafe in a Windows filename" in error for error in reporter.errors)
        )
        self.assertTrue(any("private, loopback" in error for error in reporter.errors))

    def test_encoded_control_character_in_url_is_rejected(self):
        error = validator.validate_download_url("https://example.com/plugin%0d%0a.zip")
        self.assertIn("encoded control", error)

    def test_reversed_version_range_is_rejected(self):
        reporter = self.reporter()
        validator._validate_version_range("[9.0,8.0]", "range", reporter)
        self.assertTrue(any("greater than" in error for error in reporter.errors))

    def test_safe_root_dll_is_extracted(self):
        archive = make_zip([("ExamplePlugin.dll", b"MZ-safe")])
        with tempfile.TemporaryDirectory() as directory:
            destination = validator.extract_plugin_dll(
                archive, "ExamplePlugin", directory
            )
            self.assertEqual(destination.parent, Path(directory).resolve())
            self.assertEqual(destination.read_bytes(), b"MZ-safe")

    def test_archive_with_traversal_member_is_rejected(self):
        archive = make_zip(
            [("ExamplePlugin.dll", b"MZ-safe"), ("../outside.txt", b"not extracted")]
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                validator.PackageValidationError, "unsafe path"
            ):
                validator.extract_plugin_dll(archive, "ExamplePlugin", directory)
            self.assertFalse((Path(directory).parent / "outside.txt").exists())

    def test_archive_with_duplicate_case_insensitive_dll_is_rejected(self):
        archive = make_zip(
            [("ExamplePlugin.dll", b"first"), ("exampleplugin.DLL", b"second")]
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                validator.PackageValidationError, "multiple case-insensitive"
            ):
                validator.extract_plugin_dll(archive, "ExamplePlugin", directory)

    def test_archive_with_symbolic_link_is_rejected(self):
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = make_zip([("ExamplePlugin.dll", b"MZ-safe"), (link, "target")])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                validator.PackageValidationError, "symbolic link"
            ):
                validator.extract_plugin_dll(archive, "ExamplePlugin", directory)

    def test_dll_size_limit_is_enforced_before_extraction(self):
        archive = make_zip([("ExamplePlugin.dll", b"12345")])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(validator, "MAX_DLL_BYTES", 4),
        ):
            with self.assertRaisesRegex(validator.PackageValidationError, "DLL size"):
                validator.extract_plugin_dll(archive, "ExamplePlugin", directory)

    def test_streaming_download_failure_is_reported_as_package_error(self):
        response = requests.Response()
        response.status_code = 200
        response.url = "https://example.com/plugin.zip"
        response.headers = {}
        response.history = []
        response.raw = mock.Mock()
        response.iter_content = mock.Mock(
            side_effect=requests.ConnectionError("connection lost")
        )
        session = mock.Mock()
        session.get.return_value = response

        with mock.patch.object(validator, "_resolve_hostname_is_public"):
            with self.assertRaisesRegex(
                validator.PackageValidationError, "failed while reading"
            ):
                validator._download_archive(make_plugin(), session)

    def test_redirect_is_validated_before_the_next_request(self):
        response = requests.Response()
        response.status_code = 302
        response.url = "https://example.com/plugin.zip"
        response.headers = {"Location": "http://127.0.0.1/internal"}
        response.raw = mock.Mock()
        session = mock.Mock()
        session.get.return_value = response

        with mock.patch.object(validator, "_resolve_hostname_is_public"):
            with self.assertRaisesRegex(
                validator.PackageValidationError, "unsafe download URL"
            ):
                validator._open_safe_response(response.url, session)
        session.get.assert_called_once()

    def test_download_session_retries_transient_failures(self):
        with validator.create_download_session() as session:
            retries = session.get_adapter("https://").max_retries

        self.assertEqual(3, retries.total)
        self.assertEqual(2, retries.backoff_factor)
        self.assertEqual(
            {408, 425, 429, 500, 502, 503, 504},
            set(retries.status_forcelist),
        )
        self.assertEqual(frozenset({"GET"}), retries.allowed_methods)
        self.assertFalse(retries.raise_on_status)

    def test_dll_version_reader_failure_is_reported_as_package_error(self):
        with mock.patch.object(
            validator, "get_version_number", side_effect=RuntimeError("bad resource")
        ):
            with self.assertRaisesRegex(
                validator.PackageValidationError, "could not read DLL version"
            ):
                validator.validate_dll_version("plugin.dll", "1.2.3")

    def test_generated_markdown_escapes_manifest_html_and_table_markup(self):
        catalog = make_catalog(
            make_plugin(
                **{
                    "display-name": "Unsafe | Name",
                    "description": "<script>alert(1)</script> | still text",
                }
            )
        )
        markdown = validator.gen_pl_table_from_catalog(catalog)
        self.assertIn("Unsafe &#124; Name", markdown)
        self.assertIn(
            "&lt;script&gt;alert(1)&lt;/script&gt; &#124; still text", markdown
        )
        self.assertNotIn("<script>", markdown)

    def test_generated_markdown_does_not_double_escape_existing_entities(self):
        markdown = validator.gen_pl_table_from_catalog(
            make_catalog(make_plugin(description="one &gt; zero"))
        )
        self.assertIn("one &gt; zero", markdown)
        self.assertNotIn("&amp;gt;", markdown)

    def test_sorting_preserves_double_spaces_inside_values(self):
        catalog = make_catalog(
            make_plugin(**{"display-name": "Zulu", "description": "keep  two spaces"}),
            make_plugin(
                **{
                    "folder-name": "Alpha",
                    "display-name": "Alpha",
                    "id": "b" * 64,
                    "repository": "https://example.org/alpha.zip",
                }
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_catalog(directory, catalog)
            validator.sort_catalog_file(path)
            sorted_catalog = validator.load_json_file(path)
        self.assertEqual(
            ["Alpha", "Zulu"],
            [p["display-name"] for p in sorted_catalog["npp-plugins"]],
        )
        self.assertEqual(
            "keep  two spaces", sorted_catalog["npp-plugins"][1]["description"]
        )

    def test_sort_command_does_not_rewrite_an_invalid_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            original = '{"name": "npp-pluginList"}'
            path.write_text(original, encoding="utf-8")
            paths = validator.CatalogPaths(path, Path(directory) / "doc.md", "64")
            quiet_reporter = self.reporter()
            with (
                mock.patch.object(validator, "ARCHITECTURES", {"x64": paths}),
                mock.patch.object(validator, "Reporter", return_value=quiet_reporter),
            ):
                self.assertEqual(2, validator.main(["sort"]))
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_offline_main_never_constructs_a_network_session(self):
        with (
            mock.patch.object(
                validator.requests, "Session", side_effect=AssertionError("network")
            ),
            mock.patch.object(validator, "check_generated_document"),
        ):
            self.assertEqual(0, validator.main(["all", "--offline"]))


if __name__ == "__main__":
    unittest.main()
