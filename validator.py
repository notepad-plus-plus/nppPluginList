import argparse
import html
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

import requests
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "pl.schema"
REQUEST_TIMEOUT = (10, 30)
REPORT_TIMEOUT = (5, 10)
DOWNLOAD_DEADLINE_SECONDS = 180
MAX_REDIRECTS = 5
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_DLL_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_COMPRESSION_RATIO = 1_000
COPY_CHUNK_BYTES = 1024 * 1024
USER_AGENT = "nppPluginList-validator/1"

C_SUM_LEN = 100
TMPL_BR = "<br>"
TMPL_NEW_LINE = "\n"
TMPL_TAB_HEAD = """| Plugin name | Author | Homepage | Version and link | Description |
|---|---|---|---|---|
"""


@dataclass(frozen=True)
class CatalogPaths:
    json_path: Path
    doc_path: Path
    manifest_arch: str


ARCHITECTURES = {
    "x86": CatalogPaths(
        ROOT / "src/pl.x86.json", ROOT / "doc/plugin_list_x86.md", "32"
    ),
    "x64": CatalogPaths(
        ROOT / "src/pl.x64.json", ROOT / "doc/plugin_list_x64.md", "64"
    ),
    "arm64": CatalogPaths(
        ROOT / "src/pl.arm64.json", ROOT / "doc/plugin_list_arm64.md", "arm64"
    ),
}

VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){0,3}$")
VERSION_RANGE_PATTERN = re.compile(
    r"^\[(?P<minimum>\d+(?:\.\d+){0,3})?,(?P<maximum>\d+(?:\.\d+){0,3})?\]$"
)
WINDOWS_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class DuplicateJsonKeyError(ValueError):
    pass


class PackageValidationError(ValueError):
    pass


class Reporter:
    def __init__(self, api_url=None, stream=None):
        self.api_url = (
            api_url if api_url is not None else os.environ.get("APPVEYOR_API_URL")
        )
        self.stream = stream if stream is not None else sys.stderr
        self.errors = []

    @property
    def has_errors(self):
        return bool(self.errors)

    def error(self, message):
        message = str(message)
        self.errors.append(message)
        payload = {"message": message, "category": "error", "details": ""}

        if not self.api_url:
            print(f"ERROR: {message}", file=self.stream)
            return

        endpoint = self.api_url.rstrip("/") + "/api/build/messages"
        try:
            requests.post(endpoint, json=payload, timeout=REPORT_TIMEOUT)
        except requests.RequestException as exc:
            print(f"ERROR: {message}", file=self.stream)
            print(
                f"ERROR: could not report the error to AppVeyor: {exc}",
                file=self.stream,
            )


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json_file(filename):
    with Path(filename).open(encoding="utf-8") as input_file:
        return json.load(input_file, object_pairs_hook=_reject_duplicate_json_keys)


def build_schema_validator(reporter):
    try:
        schema_data = load_json_file(SCHEMA_PATH)
        Draft202012Validator.check_schema(schema_data)
        return Draft202012Validator(schema_data, format_checker=FormatChecker())
    except (OSError, ValueError) as exc:
        reporter.error(f"{SCHEMA_PATH.name}: {exc}")
        return None


def _json_path(error):
    if not error.absolute_path:
        return "$"
    result = "$"
    for part in error.absolute_path:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def _version_tuple(version):
    parts = tuple(int(part) for part in version.split("."))
    return parts + (0,) * (4 - len(parts))


def _validate_version_range(value, label, reporter):
    match = VERSION_RANGE_PATTERN.fullmatch(value)
    if not match:
        return

    minimum = match.group("minimum")
    maximum = match.group("maximum")
    if not minimum and not maximum:
        reporter.error(f"{label}: an empty version range is not useful")
    elif minimum and maximum and _version_tuple(minimum) > _version_tuple(maximum):
        reporter.error(
            f"{label}: minimum version {minimum} is greater than maximum {maximum}"
        )


def validate_folder_name(folder_name):
    if folder_name in {".", ".."}:
        return "must not be '.' or '..'"
    if folder_name != folder_name.strip():
        return "must not have leading or trailing whitespace"
    if folder_name.endswith("."):
        return "must not end with a dot"
    if WINDOWS_INVALID_FILENAME_CHARS.search(folder_name):
        return "contains a character that is unsafe in a Windows filename"
    if folder_name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        return "is a reserved Windows filename"
    if len(folder_name) > 200:
        return "is too long"
    return None


def validate_download_url(url):
    if any(character.isspace() or ord(character) < 32 for character in url):
        return "contains whitespace or a control character"

    decoded_url = unquote(url)
    if any(ord(character) < 32 for character in decoded_url):
        return "contains an encoded control character"

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        return str(exc)

    if parsed.scheme.lower() != "https":
        return "must use HTTPS"
    if not parsed.hostname:
        return "must contain a hostname"
    if parsed.username is not None or parsed.password is not None:
        return "must not contain user information"
    if parsed.fragment:
        return "must not contain a fragment"
    if port not in (None, 443):
        return "must use the default HTTPS port"

    hostname = parsed.hostname.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or "." not in hostname
    ):
        return "must use a public hostname"
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    if not address.is_global:
        return "must not target a private, loopback, link-local, or reserved address"
    return None


def _canonical_repository_url(url):
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=hostname).geturl()


def _validate_unique_plugins(plugins, reporter):
    fields = {
        "display-name": lambda value: value.casefold(),
        "folder-name": lambda value: value.casefold(),
        "repository": _canonical_repository_url,
        "id": str.lower,
    }
    for field, normalizer in fields.items():
        seen = {}
        for index, plugin in enumerate(plugins):
            value = plugin[field]
            normalized = normalizer(value)
            if normalized in seen:
                reporter.error(
                    f"npp-plugins[{index}].{field}: duplicate of entry {seen[normalized]} ({value!r})"
                )
            else:
                seen[normalized] = index


def validate_catalog(
    filename, expected_arch, schema_validator, reporter, require_sorted=True
):
    filename = Path(filename)
    error_count = len(reporter.errors)
    try:
        catalog = load_json_file(filename)
    except (OSError, ValueError) as exc:
        reporter.error(f"{filename}: {exc}")
        return None

    schema_errors = sorted(
        schema_validator.iter_errors(catalog), key=lambda item: list(item.absolute_path)
    )
    for error in schema_errors:
        reporter.error(f"{filename.name}:{_json_path(error)}: {error.message}")
    if schema_errors:
        return None

    if catalog["arch"] != expected_arch:
        reporter.error(
            f"{filename.name}: arch is {catalog['arch']!r}, expected {expected_arch!r} for this filename"
        )

    plugins = catalog["npp-plugins"]
    _validate_unique_plugins(plugins, reporter)

    if require_sorted:
        actual_names = [plugin["display-name"] for plugin in plugins]
        expected_names = sorted(
            actual_names, key=lambda value: (value.casefold(), value)
        )
        if actual_names != expected_names:
            mismatch = next(
                index
                for index, (actual, expected) in enumerate(
                    zip(actual_names, expected_names)
                )
                if actual != expected
            )
            reporter.error(
                f"{filename.name}: plugin list is not sorted at entry {mismatch}: "
                f"found {actual_names[mismatch]!r}, expected {expected_names[mismatch]!r}"
            )

    for index, plugin in enumerate(plugins):
        folder_error = validate_folder_name(plugin["folder-name"])
        if folder_error:
            reporter.error(
                f"{filename.name}:npp-plugins[{index}].folder-name {folder_error}"
            )

        url_error = validate_download_url(plugin["repository"])
        if url_error:
            reporter.error(
                f"{filename.name}:npp-plugins[{index}].repository {url_error}"
            )

        if "npp-compatible-versions" in plugin:
            _validate_version_range(
                plugin["npp-compatible-versions"],
                f"{filename.name}:npp-plugins[{index}].npp-compatible-versions",
                reporter,
            )
        if "old-versions-compatibility" in plugin:
            match = re.fullmatch(
                r"(\[[^]]*,[^]]*\])(\[[^]]*,[^]]*\])",
                plugin["old-versions-compatibility"],
            )
            if match:
                _validate_version_range(
                    match.group(1),
                    f"{filename.name}:npp-plugins[{index}].old-versions-compatibility plugin range",
                    reporter,
                )
                _validate_version_range(
                    match.group(2),
                    f"{filename.name}:npp-plugins[{index}].old-versions-compatibility Notepad++ range",
                    reporter,
                )

    return catalog if len(reporter.errors) == error_count else None


def format_version_range(version_range):
    match = VERSION_RANGE_PATTERN.fullmatch(version_range)
    if not match:
        return version_range

    minimum = match.group("minimum")
    maximum = match.group("maximum")
    if minimum and maximum:
        return f"{minimum} - {maximum}"
    if minimum:
        return f">= {minimum}"
    if maximum:
        return f"<= {maximum}"
    return version_range


def format_old_versions_compatibility(compatibility):
    match = re.fullmatch(r"(\[[^]]*,[^]]*\])(\[[^]]*,[^]]*\])", compatibility)
    if not match:
        return compatibility
    return (
        f"older plugin versions {format_version_range(match.group(1))} "
        f"for Npp {format_version_range(match.group(2))}"
    )


def first_two_lines(description):
    if len(description) <= C_SUM_LEN:
        return ""
    break_position = description.rfind(TMPL_BR, 0, C_SUM_LEN)
    if break_position != -1:
        return description[:break_position]
    break_position = description.rfind(" ", 0, C_SUM_LEN)
    if break_position != -1:
        return description[:break_position]
    return description[:C_SUM_LEN]


def _markdown_text(value, preserve_line_breaks=False):
    # Catalog descriptions historically contain a mix of literal characters and
    # pre-escaped entities. Normalize once before escaping so generation is both
    # safe and idempotent instead of turning &gt; into &amp;gt; on every release.
    value = html.unescape(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = html.escape(value, quote=False).replace("|", "&#124;")
    return value.replace("\n", TMPL_BR if preserve_line_breaks else " ")


def _markdown_link_url(url):
    return (
        url.replace("\\", "%5C")
        .replace(" ", "%20")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("<", "%3C")
        .replace(">", "%3E")
    )


def gen_pl_table_from_catalog(catalog):
    arch = catalog["arch"]
    lines = [
        f"## Plugin List - {arch} bit",
        "",
        f"version {catalog['version']}",
        "",
        TMPL_TAB_HEAD.rstrip("\n"),
    ]

    for plugin in catalog["npp-plugins"]:
        display_name = _markdown_text(plugin["display-name"])
        author = _markdown_text(plugin["author"])
        homepage = _markdown_text(plugin["homepage"])
        repository = _markdown_link_url(plugin["repository"])
        description = _markdown_text(plugin["description"], preserve_line_breaks=True)
        summary = first_two_lines(description)
        rest = description[len(summary) :]
        if summary:
            description_cell = (
                f" <details> <summary> {summary} </summary> {rest} </details>"
            )
        else:
            description_cell = rest
        lines.append(
            f"| {display_name} | {author} | {homepage} | "
            f"[{plugin['version']} - {arch} bit]({repository}) | {description_cell} |"
        )
    return "\n".join(lines) + "\n"


def gen_pl_table(filename):
    return gen_pl_table_from_catalog(load_json_file(filename))


def _atomic_write_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_generated_document(catalog, output_path):
    _atomic_write_text(output_path, gen_pl_table_from_catalog(catalog))


def check_generated_document(catalog, output_path, reporter):
    expected = gen_pl_table_from_catalog(catalog)
    try:
        actual = Path(output_path).read_text(encoding="utf-8")
    except OSError as exc:
        reporter.error(f"{output_path}: {exc}")
        return
    if actual.replace("\r\n", "\n") != expected:
        reporter.error(
            f"{Path(output_path).name}: generated documentation is out of date"
        )


def sort_catalog_file(path):
    catalog = load_json_file(path)
    catalog["npp-plugins"] = sorted(
        catalog["npp-plugins"],
        key=lambda plugin: (plugin["display-name"].casefold(), plugin["display-name"]),
    )
    content = json.dumps(catalog, ensure_ascii=False, indent="\t") + "\n"
    _atomic_write_text(path, content)


def _resolve_hostname_is_public(url):
    hostname = urlsplit(url).hostname
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise PackageValidationError(f"could not resolve {hostname}: {exc}") from exc
    if not addresses:
        raise PackageValidationError(f"could not resolve {hostname}")
    for address_text in addresses:
        address = ipaddress.ip_address(address_text)
        if not address.is_global:
            raise PackageValidationError(
                f"{hostname} resolved to non-public address {address}"
            )


def _open_safe_response(url, session):
    current_url = url
    redirect_statuses = {301, 302, 303, 307, 308}
    for redirect_count in range(MAX_REDIRECTS + 1):
        url_error = validate_download_url(current_url)
        if url_error:
            raise PackageValidationError(
                f"unsafe download URL {current_url!r}: {url_error}"
            )
        _resolve_hostname_is_public(current_url)

        try:
            response = session.get(
                current_url,
                stream=True,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/zip, application/octet-stream",
                },
            )
        except requests.RequestException as exc:
            raise PackageValidationError(f"download failed: {exc}") from exc

        if response.status_code not in redirect_statuses:
            return response

        location = response.headers.get("Location")
        response.close()
        if not location:
            raise PackageValidationError(
                "download redirect is missing a Location header"
            )
        if redirect_count == MAX_REDIRECTS:
            raise PackageValidationError(f"download exceeded {MAX_REDIRECTS} redirects")
        current_url = urljoin(response.url or current_url, location)

    raise AssertionError("unreachable redirect loop")


def _download_archive(plugin, session):
    start_time = time.monotonic()
    response = _open_safe_response(plugin["repository"], session)

    with response:
        if response.status_code != requests.codes.ok:
            raise PackageValidationError(
                f"download returned HTTP {response.status_code}"
            )

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise PackageValidationError(
                    "download has an invalid Content-Length header"
                ) from exc
            if declared_size < 0 or declared_size > MAX_ARCHIVE_BYTES:
                raise PackageValidationError(
                    f"archive size {declared_size} exceeds the {MAX_ARCHIVE_BYTES}-byte limit"
                )

        archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        digest = sha256()
        downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=COPY_CHUNK_BYTES):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > MAX_ARCHIVE_BYTES:
                    raise PackageValidationError(
                        f"archive exceeds the {MAX_ARCHIVE_BYTES}-byte download limit"
                    )
                if time.monotonic() - start_time > DOWNLOAD_DEADLINE_SECONDS:
                    raise PackageValidationError(
                        f"download exceeded the {DOWNLOAD_DEADLINE_SECONDS}-second deadline"
                    )
                digest.update(chunk)
                archive.write(chunk)

            actual_hash = digest.hexdigest()
            if actual_hash.lower() != plugin["id"].lower():
                raise PackageValidationError(
                    f"invalid hash: got {actual_hash.lower()} but expected {plugin['id']}"
                )
            archive.seek(0)
            return archive
        except requests.RequestException as exc:
            archive.close()
            raise PackageValidationError(
                f"download failed while reading the response: {exc}"
            ) from exc
        except Exception:
            archive.close()
            raise


def _validate_archive_members(plugin_zip):
    members = plugin_zip.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise PackageValidationError(
            f"archive contains more than {MAX_ARCHIVE_MEMBERS} entries"
        )

    total_size = 0
    for member in members:
        normalized_name = member.filename.replace("\\", "/")
        path = PurePosixPath(normalized_name)
        if (
            not normalized_name
            or "\x00" in normalized_name
            or normalized_name.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized_name)
            or ".." in path.parts
        ):
            raise PackageValidationError(
                f"archive contains unsafe path {member.filename!r}"
            )
        if member.flag_bits & 0x1:
            raise PackageValidationError(
                f"archive contains encrypted entry {member.filename!r}"
            )
        if stat.S_ISLNK(member.external_attr >> 16):
            raise PackageValidationError(
                f"archive contains symbolic link {member.filename!r}"
            )
        if member.file_size < 0 or member.compress_size < 0:
            raise PackageValidationError(
                f"archive contains invalid size metadata for {member.filename!r}"
            )

        total_size += member.file_size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise PackageValidationError(
                f"archive expands beyond the {MAX_TOTAL_UNCOMPRESSED_BYTES}-byte limit"
            )
        if (
            member.file_size > COPY_CHUNK_BYTES
            and member.compress_size > 0
            and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise PackageValidationError(
                f"archive entry {member.filename!r} has an unsafe compression ratio"
            )
    return members


def extract_plugin_dll(archive, folder_name, destination_directory):
    folder_error = validate_folder_name(folder_name)
    if folder_error:
        raise PackageValidationError(
            f"unsafe plugin folder name {folder_name!r}: {folder_error}"
        )

    expected_name = f"{folder_name}.dll"
    try:
        with zipfile.ZipFile(archive) as plugin_zip:
            members = _validate_archive_members(plugin_zip)
            matches = [
                member
                for member in members
                if not member.is_dir()
                and member.filename.casefold() == expected_name.casefold()
            ]
            if not matches:
                raise PackageValidationError(
                    f"zip file does not contain root-level {expected_name}"
                )
            if len(matches) != 1:
                raise PackageValidationError(
                    f"zip file contains multiple case-insensitive copies of {expected_name}"
                )

            member = matches[0]
            if member.file_size > MAX_DLL_BYTES:
                raise PackageValidationError(
                    f"DLL size {member.file_size} exceeds the {MAX_DLL_BYTES}-byte limit"
                )

            destination_directory = Path(destination_directory).resolve()
            destination_directory.mkdir(parents=True, exist_ok=True)
            destination = (destination_directory / Path(member.filename).name).resolve()
            if destination.parent != destination_directory:
                raise PackageValidationError(
                    f"unsafe DLL destination for {member.filename!r}"
                )

            copied = 0
            with plugin_zip.open(member) as source, destination.open("wb") as output:
                while chunk := source.read(COPY_CHUNK_BYTES):
                    copied += len(chunk)
                    if copied > MAX_DLL_BYTES:
                        raise PackageValidationError(
                            f"DLL exceeds the {MAX_DLL_BYTES}-byte extraction limit"
                        )
                    output.write(chunk)
            if copied != member.file_size:
                raise PackageValidationError(
                    f"DLL size mismatch: extracted {copied} bytes, expected {member.file_size}"
                )
            return destination
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise PackageValidationError(f"invalid zip file: {exc}") from exc


def get_version_number(filename):
    from win32api import GetFileVersionInfo, HIWORD, LOWORD

    info = GetFileVersionInfo(str(filename), "\\")
    ms = info["FileVersionMS"]
    ls = info["FileVersionLS"]
    return ".".join(map(str, [HIWORD(ms), LOWORD(ms), HIWORD(ls), LOWORD(ls)]))


def _normalized_four_part_version(version):
    if not VERSION_PATTERN.fullmatch(version):
        raise PackageValidationError(f"invalid version {version!r}")
    return version + (3 - version.count(".")) * ".0"


def validate_dll_version(dll_path, expected_version):
    try:
        dll_version = get_version_number(dll_path)
    except Exception as exc:
        raise PackageValidationError(
            f"could not read DLL version information: {exc}"
        ) from exc

    normalized_expected_version = _normalized_four_part_version(expected_version)
    if dll_version != normalized_expected_version:
        raise PackageValidationError(
            f"unexpected DLL version {dll_version}; expected {normalized_expected_version}"
        )


def validate_remote_catalog(catalog, architecture, reporter):
    with tempfile.TemporaryDirectory(
        prefix=f"npp-plugin-list-{architecture}-"
    ) as temporary_directory:
        with requests.Session() as session:
            session.max_redirects = MAX_REDIRECTS
            for plugin in catalog["npp-plugins"]:
                print(plugin["display-name"], end="")
                compatibility_messages = []
                if "npp-compatible-versions" in plugin:
                    compatibility_messages.append(
                        f"REQUIRES Npp {format_version_range(plugin['npp-compatible-versions'])}"
                    )
                if "old-versions-compatibility" in plugin:
                    compatibility_messages.append(
                        format_old_versions_compatibility(
                            plugin["old-versions-compatibility"]
                        )
                    )
                print(
                    f" *** {'; '.join(compatibility_messages)} ***"
                    if compatibility_messages
                    else ""
                )

                archive = None
                try:
                    archive = _download_archive(plugin, session)
                    dll_path = extract_plugin_dll(
                        archive, plugin["folder-name"], temporary_directory
                    )
                    validate_dll_version(dll_path, plugin["version"])
                except (OSError, PackageValidationError, KeyError, TypeError) as exc:
                    reporter.error(f"{plugin['display-name']}: {exc}")
                finally:
                    if archive is not None:
                        archive.close()


def validate_local_catalogs(
    architectures, reporter, check_docs=False, require_sorted=True
):
    schema_validator = build_schema_validator(reporter)
    if schema_validator is None:
        return {}

    catalogs = {}
    versions = {}
    for architecture in architectures:
        paths = ARCHITECTURES[architecture]
        catalog = validate_catalog(
            paths.json_path,
            paths.manifest_arch,
            schema_validator,
            reporter,
            require_sorted=require_sorted,
        )
        if catalog is not None:
            catalogs[architecture] = catalog
            versions[architecture] = catalog["version"]
            if check_docs:
                check_generated_document(catalog, paths.doc_path, reporter)

    if len(set(versions.values())) > 1:
        reporter.error(
            "catalog versions do not match: "
            + ", ".join(
                f"{architecture}={version}"
                for architecture, version in versions.items()
            )
        )
    return catalogs


def _target_architectures(target):
    return list(ARCHITECTURES) if target in {"all", "all_md", "sort"} else [target]


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="Validate the Notepad++ plugin catalogs"
    )
    parser.add_argument(
        "target",
        nargs="?",
        choices=[*ARCHITECTURES, "all", "all_md", "sort"],
        help="architecture to validate, all catalogs, generated Markdown, or deterministic sorting",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="perform schema, semantic, duplicate, URL and generated-document checks without downloads",
    )
    return parser


def main(argv=None):
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    target = args.target
    if target is None:
        options = ", ".join([*ARCHITECTURES, "all", "all_md", "sort"])
        target = input(f"Please provide the target ({options}): ").strip().lower()
        if target not in {*ARCHITECTURES, "all", "all_md", "sort"}:
            parser.error(f"unknown target: {target}")

    architectures = _target_architectures(target)
    reporter = Reporter()

    if target == "sort":
        catalogs = validate_local_catalogs(
            architectures, reporter, require_sorted=False
        )
        if reporter.has_errors or len(catalogs) != len(architectures):
            return 2
        for architecture in architectures:
            sort_catalog_file(ARCHITECTURES[architecture].json_path)
        sorted_catalogs = validate_local_catalogs(architectures, reporter)
        return (
            2
            if reporter.has_errors or len(sorted_catalogs) != len(architectures)
            else 0
        )

    catalogs = validate_local_catalogs(
        architectures,
        reporter,
        check_docs=args.offline,
    )
    if reporter.has_errors or len(catalogs) != len(architectures):
        return 2

    if target == "all_md":
        for architecture, catalog in catalogs.items():
            write_generated_document(catalog, ARCHITECTURES[architecture].doc_path)
        return 0

    if args.offline:
        return 0

    for architecture, catalog in catalogs.items():
        print(f"Provided architecture: {architecture}.")
        validate_remote_catalog(catalog, architecture, reporter)
    if reporter.has_errors:
        return 2

    for architecture, catalog in catalogs.items():
        write_generated_document(catalog, ARCHITECTURES[architecture].doc_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
