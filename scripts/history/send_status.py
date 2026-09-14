#!/usr/bin/env python3
"""Read observed saved-send progress. No prompt or credential is printed."""
import argparse
import json
import sys
import urllib.request
from cli import Client

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--descriptor', required=True)
    args = parser.parse_args()
    try:
        client = Client(args.descriptor)
        body = json.loads(sys.stdin.buffer.read(2048))
        req = urllib.request.Request(client.endpoint.rstrip('/') + '/v1/saved/status', data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + client.token, 'Content-Type': 'application/json'})
        with client.opener.open(req, timeout=10) as response:
            print(response.read(2048).decode())
    except Exception:
        print('{"version":1,"phase":"unavailable"}')
        sys.exit(1)
