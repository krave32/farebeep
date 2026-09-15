"""Repoint the WhatsApp app webhook from the dead cloudflare tunnel to
the Railway deployment, then show the resulting config."""
import json
import urllib.parse
import urllib.request
import urllib.error
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from meta_flow_setup import load_env  # noqa: E402

env = load_env()
ver = env["META_API_VERSION"]
APP_ID = "1991607382228060"
app_tok = f"{APP_ID}|{env['META_APP_SECRET']}"
CB = "https://web-production-374ef.up.railway.app/webhook/meta"

params = urllib.parse.urlencode({
    "access_token": app_tok,
    "object": "whatsapp_business_account",
    "callback_url": CB,
    "verify_token": env["META_VERIFY_TOKEN"],
    "fields": "messages",
})
req = urllib.request.Request(
    f"https://graph.facebook.com/{ver}/{APP_ID}/subscriptions?{params}",
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print("repoint:", json.loads(r.read()))
except urllib.error.HTTPError as e:
    print("repoint FAILED:", e.read().decode())
    sys.exit(1)

# verify - Meta pings callback_url with the verify token on subscribe;
# read back current config
url = (f"https://graph.facebook.com/{ver}/{APP_ID}/subscriptions"
       f"?access_token={urllib.parse.quote(app_tok)}")
with urllib.request.urlopen(url, timeout=30) as r:
    print("current:", json.dumps(json.loads(r.read()), indent=1))
