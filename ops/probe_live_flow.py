"""Simulate Meta's encrypted /flow requests against the LIVE Railway
endpoint using the local keypair (public half encrypts, private half
is what the server decrypts with). Prints decrypted responses."""
import base64
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE = "https://web-production-374ef.up.railway.app/flow"
priv = serialization.load_pem_private_key(
    Path(__file__).parent.parent.joinpath("flow_private_key.pem").read_bytes(),
    password=None)
pub = priv.public_key()


def call(payload: dict):
    aes_key, iv = base64.b64encode(b""), None
    aes_key, iv = __import__("os").urandom(32), __import__("os").urandom(16)
    blob = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode(), None)
    wrapped = pub.encrypt(aes_key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(), label=None))
    envelope = {
        "encrypted_flow_data": base64.b64encode(blob).decode(),
        "encrypted_aes_key": base64.b64encode(wrapped).decode(),
        "initial_vector": base64.b64encode(iv).decode(),
    }
    req = urllib.request.Request(
        BASE, data=json.dumps(envelope).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:300]}")
        return
    flipped = bytes(b ^ 0xFF for b in iv)
    try:
        out = json.loads(AESGCM(aes_key).decrypt(
            flipped, base64.b64decode(body), None))
    except Exception as e:
        print(f"  decrypt failed ({e}); raw[:60]={body[:60]!r}")
        return
    print(f"  HTTP {status}: {json.dumps(out)}")


for payload in (
    {"version": "3.0", "action": "ping"},
    {"version": "3.0", "action": "INIT", "screen": "",
     "flow_token": "probe"},
):
    print(f"-> {payload['action']}:")
    call(payload)
