import json
import os
import sys
from hashlib import sha256
import requests
from jsonschema import Draft4Validator, FormatChecker

api_url = os.environ.get('APPVEYOR_API_URL')
has_error = False

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


    for plugin in pl["npp-plugins"]:
        try:
            response = requests.get(plugin["repository"])
        except requests.exceptions.RequestException as e:
            post_error(str(e))
            continue

        if response.status_code != 200:
            post_error(f'{plugin["display-name"]}: failed to download plugin. Returned code {response.status_code}')
            continue

        hash = sha256(response.content).hexdigest()
        if plugin["id"].lower() != hash.lower():
            post_error(f'{plugin["display-name"]}: Invalid hash. Got {hash.lower()} but expected {plugin["id"]}')
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
