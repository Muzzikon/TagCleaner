"""Bounded result batches and lossless compact worker transport."""
import base64
import json
import zlib

RESULT_BATCH_SIZE = 100


def encode_event(event):
    payload=json.dumps(event,ensure_ascii=False,default=str)
    if len(payload)<256*1024:
        return payload
    return json.dumps({'kind':'compressed','payload':base64.b64encode(
        zlib.compress(payload.encode('utf-8'),1)).decode('ascii')})


def decode_event(line):
    event=json.loads(line)
    if event.get('kind')=='compressed':
        event=json.loads(zlib.decompress(base64.b64decode(event['payload'],validate=True)))
    return event
