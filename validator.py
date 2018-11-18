import json
import os
import io
import sys
import requests
import zipfile
from hashlib import sha256
from jsonschema import Draft4Validator, FormatChecker
import win32api
from win32api import GetFileVersionInfo, LOWORD, HIWORD

api_url = os.environ.get('APPVEYOR_API_URL')
has_error = False

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

def parse(filename):
    try:
        schema = json.loads(open("pl.schema").read())
        schema = Draft4Validator(schema, format_checker=FormatChecker())
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


    os.mkdir("./" + bitness_from_input)
    for plugin in pl["npp-plugins"]:
        print(plugin["display-name"])

        try:
            response = requests.get(plugin["repository"], verify=False)
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
        # so find the exactfile name to use
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


bitness_from_input = sys.argv[1]
print('input: %s' % bitness_from_input)
if bitness_from_input == 'x64':
    parse("src/pl.x64.json")
else:
    parse("src/pl.x86.json")

if has_error:
    sys.exit(-2)
else:
    sys.exit()
