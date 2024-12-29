import io
import json
import os
import shutil
import sys
import zipfile
from hashlib import sha256
from urllib.parse import unquote as unquote_url

import requests
from jsonschema import Draft202012Validator, FormatChecker
from win32api import GetFileVersionInfo, LOWORD, HIWORD

api_url = os.environ.get('APPVEYOR_API_URL')
has_error = False

# Constants for creating the plugin list overview
C_LINE_BREAK = '\x0d'
C_LINE_FEED = '\x0a'
C_SPACE = ' '
C_SUM_LEN = 100
TMPL_VERT = '&vert;'
TMPL_BR = '<br>'
TMPL_NEW_LINE = '\n'
TMPL_TR_B = '| '
TMPL_TD = ' | '
TMPL_TR_E = ' |'
TMPL_TAB_HEAD = '''| Plugin name | Author | Homepage | Version and link | Description |
|---|---|---|---|---|
'''


def get_version_number(filename):
    info = GetFileVersionInfo(filename, "\\")
    ms = info['FileVersionMS']
    ls = info['FileVersionLS']
    return '.'.join(map(str, [HIWORD(ms), LOWORD(ms), HIWORD(ls), LOWORD(ls)]))


def post_error(message):
    global has_error

    has_error = True

    message = {
        "message": message,
        "category": "error",
        "details": ""
    }

    if api_url:
        requests.post(api_url + "api/build/messages", json=message)
    else:
        from pprint import pprint
        pprint(message)


def first_two_lines(description):
    if len(description) <= C_SUM_LEN:
        return ""
    i = description.rfind(TMPL_BR, 0, C_SUM_LEN)
    if i != -1:
        return description[:i]
    i = description.rfind(C_SPACE, 0, C_SUM_LEN)
    if i != -1:
        return description[:i]
    return description[:C_SUM_LEN]


def rest_of_text(description):
    return description[len(first_two_lines(description)):]


def gen_pl_table(filename):
    pl = json.loads(open(filename).read())
    arch = pl["arch"]
    tab_text = "## Plugin List - %s bit%s" % (arch, TMPL_NEW_LINE * 2)
    tab_text += "version %s%s" % (pl["version"], TMPL_NEW_LINE * 2)
    tab_text += TMPL_TAB_HEAD

    # Plugin Name = ij.display-name
    # Author = ij.author
    # Homepage = ij.homepage
    # Version and link = "[" + ij.version + " - " + json_file.arch + " bit](" + ij.repository +")"
    # Description = " <details> <summary> " + first_two_lines(ij.description) + " </summary> " rest_of_text(ij.description) +"</details>"
    for plugin in pl["npp-plugins"]:
        tab_line = TMPL_TR_B + plugin["display-name"] + TMPL_TD + plugin["author"] + TMPL_TD + plugin["homepage"] + TMPL_TD
        tab_line += "[%s - %s bit](%s)" % (plugin["version"], arch, plugin["repository"])
        tab_line += TMPL_TD
        descr = plugin["description"]
        descr = descr.replace(C_LINE_FEED, TMPL_BR).replace(C_LINE_BREAK, '').replace("|", TMPL_VERT)
        summary = first_two_lines(descr)
        rest = rest_of_text(descr)
        if summary:
            tab_line += " <details> <summary> %s </summary> %s </details>" % (summary, rest)
        else:
            tab_line += rest
        tab_line += TMPL_TR_E + TMPL_NEW_LINE
        tab_text += tab_line
    return tab_text


def parse(filename):
    try:
        schema = json.loads(open("pl.schema").read())
        schema = Draft202012Validator(schema, format_checker=FormatChecker())
    except ValueError as e:
        post_error("pl.schema - " + str(e))
        return

    try:
        pl = json.loads(open(filename).read())
    except ValueError as e:
        post_error(filename + " - " + str(e))
        return

    for error in schema.iter_errors(pl):
        post_error(error.message)

    foldernames = []
    displaynames = []
    repositories = []

    if os.path.exists("./" + provided_architecture):
        shutil.rmtree("./" + provided_architecture, True)

    os.mkdir("./" + provided_architecture)
    for plugin in pl["npp-plugins"]:
        print(plugin["display-name"], end='')

        if 'npp-compatible-versions' not in plugin:
            print()
        else:
            import re
            req_npp_version = plugin['npp-compatible-versions']
            min_re = r'^\[.*\d+,'
            max_re = r'^\[,\d+'
            min_version = re.match(min_re, req_npp_version)
            max_version = re.match(max_re, req_npp_version)
            try:
                if min_version is not None:
                    req_npp_version = re.sub(min_re, f'{min_version.group(0)[:-1]} - ', req_npp_version)
                if max_version is not None:
                    req_npp_version = re.sub(max_re, f'<= {max_version.group(0)[2:]}', req_npp_version)
            except:
                pass

            req_npp_version = re.sub(r'\s{2,}', ' ', re.sub(r'\[', '', re.sub(r'\]', '', req_npp_version)))
            print(f' *** REQUIRES Npp {req_npp_version.strip()} ***')

        try:
            response = requests.get(unquote_url(plugin["repository"]))
        except requests.exceptions.RequestException as e:
            post_error(f'{plugin["display-name"]}: {str(e)}')
            continue

        if response.status_code != 200:
            post_error(f'{plugin["display-name"]}: failed to download plugin. Returned code {response.status_code}.')
            continue

        # Calculate the hash and compare it with the expected value
        hash = sha256(response.content).hexdigest()
        if plugin["id"].lower() != hash.lower():
            post_error(f'{plugin["display-name"]}: Invalid hash. Got {hash.lower()} but expected {plugin["id"]}.')
            continue

        # Ensure it is a valid zip file
        try:
            zip = zipfile.ZipFile(io.BytesIO(response.content))
        except zipfile.BadZipFile as e:
            post_error(f'{plugin["display-name"]}: Invalid zip file.')
            continue

        # The expected DLL name
        dll_name = f'{plugin["folder-name"]}.dll'.lower()

        # While Notepad++ is not case-sensitive, extracting files from the zip file is, so we convert
        # both the expected file name and the extracted file names to lowercase for accurate matching
        for file in zip.namelist():
            if dll_name == file.lower():
                dll_name = file
                break
        else:
            post_error(f'{plugin["display-name"]}: Zip file does not contain {plugin["folder-name"]}.dll.')
            continue

        with zip.open(dll_name) as dll_file, open("./" + provided_architecture + "/" + dll_name, 'wb') as f:
            f.write(dll_file.read())

        version = plugin["version"]

        # Fill in any missing numbers as zeros to ensure a complete version number
        version = version + (3 - version.count('.')) * ".0"

        try:
            dll_version = get_version_number("./" + provided_architecture + "/" + dll_name)
        except win32api.error:
            post_error(f'{plugin["display-name"]}: Does not contain any version information.')
            continue

        if dll_version != version:
            post_error(f'{plugin["display-name"]}: Unexpected DLL version. The DLL version '
                       f'detected is {dll_version}, but the expected version is {version}.')
            continue

        # Check the uniqueness of the JSON folder name, the display name, and the repository
        found = False
        for name in displaynames:
            if plugin["display-name"] == name:
                post_error(f'{plugin["display-name"]}: non unique display-name entry.')
                found = True
        if not found:
            displaynames.append(plugin["display-name"])

        found = False
        for folder in foldernames:
            if plugin["folder-name"] == folder:
                post_error(f'{plugin["folder-name"]}: non unique folder-name entry.')
                found = True
        if found == False:
            foldernames.append(plugin["folder-name"])

        found = False
        for repo in repositories:
            if plugin["repository"] == repo:
                post_error(f'{plugin["repository"]}: non unique repository entry.')
                found = True
        if found == False:
            repositories.append(plugin["repository"])


ARCHITECTURE_FILENAMES_MAPPING = {
    'x86': ("src/pl.x86.json", "plugin_list_x86.md"),
    'x64': ("src/pl.x64.json", "plugin_list_x64.md"),
    'arm64': ("src/pl.arm64.json", "plugin_list_arm64.md")
}
ARCHITECTURE_OPTIONS = ", ".join(ARCHITECTURE_FILENAMES_MAPPING.keys())
ARCHITECTURE_OPTIONS = f"{ARCHITECTURE_OPTIONS.rpartition(',')[0]}, or{ARCHITECTURE_OPTIONS.rpartition(',')[-1]}"
if len(sys.argv) > 1:
    provided_architecture = sys.argv[1].lower()
else:
    provided_architecture = input(f'Please provide the target architecture ({ARCHITECTURE_OPTIONS}): ').lower()
if provided_architecture in ARCHITECTURE_FILENAMES_MAPPING:
    json_file, output_file = ARCHITECTURE_FILENAMES_MAPPING[provided_architecture]
else:
    json_file, output_file = ARCHITECTURE_FILENAMES_MAPPING['x86']
print(f'Provided architecture: {provided_architecture}.')
parse(json_file)
with open(output_file, "w") as md_file:
    md_file.write(gen_pl_table(json_file))

if has_error:
    sys.exit(-2)
else:
    sys.exit()
