import json
import sys

def parse(filename):
    try:
        with open(filename) as f:
            return json.load(f)
    except ValueError as e:
        print('invalid json: %s' % e)
        raise -2
        return None # or: raise

bitness_from_input = sys.argv[1]
print('input: %s' % bitness_from_input)
if bitness_from_input == 'x64':
    parse("src/pl.x64.json")
else:
    parse("src/pl.x86.json")
