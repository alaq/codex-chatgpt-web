#!/usr/bin/env python3
"""Send one saved-conversation request from stdin; never put prompts or credentials in argv."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from cli import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--descriptor', required=True)
    args = parser.parse_args()
    client = Client(args.descriptor)
    raw = sys.stdin.buffer.read(80 * 1024 + 1)
    if len(raw) > 80 * 1024:
        raise ValueError('Saved send request too large')
    body = json.loads(raw)
    request = urllib.request.Request(client.endpoint.rstrip('/') + '/v1/saved/send', data=json.dumps(body).encode(),
                                    headers={'Authorization': 'Bearer ' + client.token, 'Content-Type': 'application/json'})
    try:
        with client.opener.open(request, timeout=260) as response:
            result = json.loads(response.read(8192))
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read(8192))
            code = data.get('error', '')
        except Exception:
            code = ''
        allowed = {'saved_send_uncertain', 'saved_send_busy', 'saved_send_source_busy', 'saved_send_source_changed',
                   'saved_send_disabled', 'saved_send_account_mismatch', 'saved_send_transaction_conflict',
                   'saved_send_composer_unavailable', 'saved_send_draft_mismatch', 'saved_send_wrong_page'}
        result = {'version': 1, 'status': 'uncertain' if code not in allowed or code == 'saved_send_uncertain' else 'not_sent',
                  'error': code if code in allowed else 'saved_send_failed'}
    except (urllib.error.URLError, TimeoutError):
        result = {'version': 1, 'status': 'uncertain', 'error': 'saved_send_transport_lost'}
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'version': 1, 'status': 'uncertain', 'error': 'saved_send_client_failed'}))
        sys.exit(1)
