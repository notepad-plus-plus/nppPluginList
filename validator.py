import json

def parse(filename):
    try:
        with open(filename) as f:
            return json.load(f)
    except ValueError as e:
        print('invalid json: %s' % e)
        raise -2
        return None # or: raise

parse("src/pl.x64.json")
parse("src/pl.x86.json")
