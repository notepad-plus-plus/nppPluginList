import json
import os
import io
import sys
import shutil

import requests
import zipfile
from hashlib import sha256
from jsonschema import Draft202012Validator, FormatChecker
import win32api
from win32api import GetFileVersionInfo, LOWORD, HIWORD

api_url = os.environ.get('APPVEYOR_API_URL')
has_error = False

skipCertCheck = [
    "SQLinForm"
]

# constants for creation of plugin list overview
c_line_break = '\x0d'
c_line_feed = '\x0a'
c_space = ' '
c_sum_len = 100
tmpl_vert = '&vert;'
tmpl_br = '<br>'
tmpl_new_line = '\n'
tmpl_tr_b = '| '
tmpl_td   = ' | '
tmpl_tr_e = ' |'
tmpl_tab_head = '''|Plugin name | Author | Homepage | Version and link | Description |
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
    if len(description) <= c_sum_len:
        return ""
    i = description.rfind(tmpl_br,0,c_sum_len)
    if i != -1:
        return description[:i]
    i = description.rfind(c_space,0,c_sum_len)
    if i != -1:
        return description[:i]
    return description[:c_sum_len]

def rest_of_text(description):
    return description[len(first_two_lines(description)):]

def gen_pl_table(filename):
    pl = json.loads(open(filename).read())
    arch = pl["arch"]
    tab_text = "## Plugin List - %s bit%s" % (arch, tmpl_new_line)
    tab_text += "version %s%s" % (pl["version"], tmpl_new_line)
    tab_text += tmpl_tab_head

    # Plugin Name = ij.display-name
    # Author = ij.author
    # Homepage = ij.homepage
    # Version and link = "[" + ij.version + " - " + json_file.arch + " bit](" + ij.repository +")"
    # Description = " <details> <summary> " + first_two_lines(ij.description) + " </summary> " rest_of_text(ij.description) +"</details>"
    for plugin in pl["npp-plugins"]:
        tab_line = tmpl_tr_b + plugin["display-name"] + tmpl_td + plugin["author"] + tmpl_td + plugin["homepage"] + tmpl_td
        tab_line += "[%s - %s bit](%s)" % (plugin["version"], arch, plugin["repository"])
        tab_line += tmpl_td
        descr = plugin["description"]
        descr = descr.replace(c_line_feed, tmpl_br).replace(c_line_break, '').replace("|", tmpl_vert)
        summary = first_two_lines(descr)
        rest = rest_of_text(descr)
        if summary:
            tab_line += " <details> <summary> %s </summary> %s </details>" % (summary, rest)
        else:
            tab_line += rest
        tab_line += tmpl_tr_e + tmpl_new_line
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

    if os.path.exists("./" + bitness_from_input):
        shutil.rmtree("./" + bitness_from_input, True)

    os.mkdir("./" + bitness_from_input)
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
            if (plugin["display-name"] in skipCertCheck):
                    response = requests.get(plugin["repository"], verify=False)
            else:  
                    response = requests.get(plugin["repository"])
            
        except requests.exceptions.RequestException as e:
            post_error(str(e))
            continue

        if response.status_code != 200:
            post_error(f'{plugin["display-name"]}: failed to download plugin. Returned code {response.status_code}')
            continue

        # Hash it and make sure its what is expected
        hash = sha256(response.content).hexdigest()
        if plugin["id"].lower() != hash.lower():
            post_error(f'{plugin["display-name"]}: Invalid hash. Got {hash.lower()} but expected {plugin["id"]}')
            continue

        # Make sure its a valid zip file
        try:
            zip = zipfile.ZipFile(io.BytesIO(response.content))
        except zipfile.BadZipFile as e:
            post_error(f'{plugin["display-name"]}: Invalid zip file')
            continue

        # The expected DLL name
        dll_name = f'{plugin["folder-name"]}.dll'.lower()

        # Notepad++ is not case sensitive, but extracting files from the zip is,
        # so find the exact file name to use
        for file in zip.namelist():
            if dll_name == file.lower():
                dll_name = file
                break
        else:
            post_error(f'{plugin["display-name"]}: Zip file does not contain {plugin["folder-name"]}.dll')
            continue

        with zip.open(dll_name) as dll_file, open("./" + bitness_from_input + "/" + dll_name, 'wb') as f:
            f.write(dll_file.read())

        version = plugin["version"]

        # Fill in any of the missing numbers as zeros
        version = version + (3 - version.count('.')) * ".0"

        try:
            dll_version = get_version_number("./" + bitness_from_input + "/" + dll_name)
        except win32api.error:
            post_error(f'{plugin["display-name"]}: Does not contain any version information')
            continue

        if dll_version != version:
            post_error(f'{plugin["display-name"]}: Unexpected DLL version. DLL is {dll_version} but expected {version}')
            continue


        #check uniqueness of json folder-name, display-name and repository
        found = False
        for name in displaynames :
           if plugin["display-name"] == name :
               post_error(f'{plugin["display-name"]}: non unique display-name entry')
               found = True
        if found == False:
               displaynames.append(plugin["display-name"])

        found = False
        for folder in foldernames :
           if plugin["folder-name"] == folder :
               post_error(f'{plugin["folder-name"]}: non unique folder-name entry')
               found = True
        if found == False:
           foldernames.append(plugin["folder-name"])

        found = False
        for repo in repositories :
           if plugin["repository"] == repo :
               post_error(f'{plugin["repository"]}: non unique repository entry')
               found = True
        if found == False:
           repositories.append(plugin["repository"])

bitness_from_input = ""
if len(sys.argv) > 1:
    bitness_from_input = sys.argv[1]
else:
    print('please provide the bitness (x86 or x64 or arm64) as the first argument')
    sys.exit(-2)
print('input: %s' % bitness_from_input)
if bitness_from_input.lower() == 'x64':
    parse("src/pl.x64.json")
    with open("plugin_list_x64.md", "w") as md_file:
        md_file.write(gen_pl_table("src/pl.x64.json"))
elif bitness_from_input.lower() == 'arm64':
    parse("src/pl.arm64.json")
    with open("plugin_list_arm64.md", "w") as md_file:
        md_file.write(gen_pl_table("src/pl.arm64.json"))
else:
    parse("src/pl.x86.json")
    with open("plugin_list_x86.md", "w") as md_file:
        md_file.write(gen_pl_table("src/pl.x86.json"))

if has_error:
    sys.exit(-2)
else:
    sys.exit()
