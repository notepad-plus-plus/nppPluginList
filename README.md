<picture>
  <source media="(prefers-color-scheme: light)" srcset="./nppPlugins.png">
  <source media="(prefers-color-scheme: dark)" srcset="./nppPluginsDark.png">
  <img alt="Notepad++ Plugin List" src="./nppPlugins.png">
</picture>

**Notepad++ Plugin List** is an official collection of [Notepad++](https://github.com/notepad-plus-plus/notepad-plus-plus) plugins. It provides a list of plugins to the built-in Plugin Admin in Notepad++ for the installation/update/deletion of plugins as users desire.

The list is in JSON format, but encapsulated in a binary (DLL), so it can be signed by a certificate to avoid being hacked. Any Notepad++ plugin is welcome to be submitted here, but please test it locally before doing your PR.

To review the current list of plugins and their features see the generated list of Plugins in either:

* [32-Bit Plugin List](doc/plugin_list_x86.md)
* [64-Bit Plugin List](doc/plugin_list_x64.md)
* [64-Bit ARM Plugin List](doc/plugin_list_arm64.md)

Here is all the information you need to perform local tests:  
https://npp-user-manual.org/docs/plugins/#plugins-admin

Please check here if you need any support:  
https://community.notepad-plus-plus.org/topic/16566/support-for-plugins-admin-npppluginlist

Local validation
----------------

The validator supports a deterministic offline mode for pull-request review. It checks the JSON schema, duplicate JSON keys, architecture metadata, case-insensitive plugin duplicates, version ranges, HTTPS package URLs, safe Windows folder names, ordering, and generated documentation without making network requests:

```powershell
python -m pip install --require-hashes -r requirements.lock
python -m unittest discover -s tests -v
python validator.py all --offline
```

Python 3.12 is the version used by CI and by the generated dependency lock. Package URL availability, package contents, SHA-256 hashes, and DLL versions require the architecture-specific online validation used by CI:

```powershell
python validator.py x86
python validator.py x64
python validator.py arm64
```

Remote packages are downloaded with size and time limits. Redirects must remain on public HTTPS endpoints. ZIP files with traversal paths, symbolic links, encryption, duplicate DLL names, excessive expansion, or a non-root plugin DLL are rejected. Only the expected DLL is copied to a temporary directory, which is removed before the validator exits.

Use `./sort_plugin_lists.ps1` for deterministic catalog ordering. It preserves plugin text verbatim. Regenerate the Markdown views atomically with `python validator.py all_md`, then rerun the offline checks.

Release safety
--------------

The signing workflow is restricted to the upstream `master` branch. A release operator must provide an exact `sign_cli_version`; floating or prerelease tool selection is not accepted. The workflow signs and verifies only the catalog DLL produced by the current matrix job, never downloaded plugin DLLs.

Build Status
------------

[![Github build status](https://github.com/notepad-plus-plus/nppPluginList/actions/workflows/CI_build.yml/badge.svg)](https://github.com/notepad-plus-plus/nppPluginList/actions/workflows/CI_build.yml)
[![GitHub release](https://img.shields.io/github/release/notepad-plus-plus/nppPluginList.svg)](https://github.com/notepad-plus-plus/nppPluginList/releases)
