import json
import os
import sys
import requests
from jsonschema import Draft4Validator, FormatChecker

api_url = os.environ['APPVEYOR_API_URL']
has_error = False

def post_error(message):
    global has_error

    has_error = True
    requests.post(api_url + "api/build/messages", json={
        "message": message,
        "category": "error",
        "details": ""
    })

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
