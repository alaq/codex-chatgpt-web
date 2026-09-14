#!/usr/bin/env python3
"""Read one account-verified attachment via the local DEV browser."""
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
        body = json.loads(sys.stdin.buffer.read(4096))
        req = urllib.request.Request(client.endpoint.rstrip('/') + '/v1/saved/media', data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + client.token, 'Content-Type': 'application/json'})
        with client.opener.open(req, timeout=150) as response:
            raw = response.read(29 * 1024 * 1024)
            if len(raw) >= 29 * 1024 * 1024: raise ValueError('oversized media envelope')
            sys.stdout.buffer.write(raw)
    except Exception:
        sys.stderr.write('saved media unavailable\n')
        sys.exit(1)
