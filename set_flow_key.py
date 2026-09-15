"""Generate the Flows RSA keypair, upload the public half to Meta
(PHONE_NUMBER_ID/whatsapp_business_encryption, form-urlencoded
business_public_key), and save the private half to flow_private_key.pem.

Usage: python set_flow_key.py
"""
import json
import sys
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from meta_flow_setup import load_env  # noqa: E402

env = load_env()
ver = env["META_API_VERSION"]

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub = key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo).decode().strip()
priv = key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode().strip()

body = urllib.parse.urlencode(
    {"business_public_key": pub}).encode()
req = urllib.request.Request(
    f"https://graph.facebook.com/{ver}/{env['META_PHONE_NUMBER_ID']}"
    "/whatsapp_business_encryption",
    data=body, method="POST",
    headers={"Authorization": f"Bearer {env['META_ACCESS_TOKEN']}",
             "Content-Type": "application/x-www-form-urlencoded"})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("Upload result:", json.loads(resp.read()))
except urllib.error.HTTPError as e:
    print(f"UPLOAD FAILED HTTP {e.code}: {e.read().decode()}")
    sys.exit(1)

Path(ROOT / "flow_private_key.pem").write_text(priv + "\n", encoding="utf-8")
print("Private key saved to flow_private_key.pem (not committed).")
