#!/usr/bin/env python3
"""Create a saved conversation with a durable source-side creation transaction."""
import argparse
import json
import sys
import urllib.request
from cli import Client

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--descriptor',required=True)
    args=parser.parse_args()
    try:
        client=Client(args.descriptor)
        raw=sys.stdin.buffer.read(80*1024+1)
        if len(raw)>80*1024:raise ValueError('oversized request')
        body=json.loads(raw)
        req=urllib.request.Request(client.endpoint.rstrip('/')+'/v1/saved/create',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+client.token,'Content-Type':'application/json'})
        with client.opener.open(req,timeout=260) as response:print(response.read(8192).decode())
    except Exception:print('{"version":1,"status":"uncertain","error":"saved_create_needs_recovery"}')
