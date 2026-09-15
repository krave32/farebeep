"""One-off Meta Flows API helper: create the Set-a-Beep flow and upload
our screens JSON. Draft-only (never publishes). Run again with an
existing flow id to refresh the screens.

Usage:
  python meta_flow_setup.py            # create flow + upload flow_screens.json
  python meta_flow_setup.py <flow_id>  # refresh screens on existing flow
"""
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
ENV_PATH = ROOT / "FareBeep" / ".env"
SCREENS = ROOT / "FareBeep" / "whatsapp" / "flow_screens.json"


def load_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    # Railway-exported variables (railenv.json) win over the local .env -
    # the deployed service carries the live token.
    rail = ROOT / "railenv.json"
    if rail.exists():
        env.update(json.loads(rail.read_text(encoding="utf-8")))
    env.update({k: v for k, v in os.environ.items() if k in
                ("META_ACCESS_TOKEN", "META_API_VERSION")})
    return env


def call(env: dict, path: str, method: str = "GET", **kw) -> dict:
    url = f"https://graph.facebook.com/{env['META_API_VERSION']}/{path}"
    req = urllib.request.Request(url, method=method, **kw)
    req.add_header("Authorization", f"Bearer {env['META_ACCESS_TOKEN']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)


def main() -> None:
    env = load_env()
    ver = env["META_API_VERSION"]

    # 1. WABA id: explicit WABA_ID env wins. Only resolved when CREATING a
    # flow - the phone-node lookup can 400 on some tokens.
    waba = os.environ.get("WABA_ID")
    if waba:
        print(f"WABA (from WABA_ID): {waba}")

    flow_id = None
    if len(sys.argv) > 1:
        flow_id = sys.argv[1]
        print(f"Using existing flow: {flow_id}")
    else:
        # WABA is only needed to CREATE a flow; the phone-node lookup can
        # 400 on some tokens, so resolve it lazily here.
        if waba is None:
            info = call(env, f"{env['META_PHONE_NUMBER_ID']}?fields=id,whatsapp_business_account")
            waba = info["whatsapp_business_account"]["id"]
            print(f"WABA (via phone node): {waba}")
        # Create the flow (draft, category OTHER)
        body = json.dumps({"name": "farebeep_set_beep", "categories": ["OTHER"]}).encode()
        req = urllib.request.Request(
            f"https://graph.facebook.com/{ver}/{waba}/flows",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {env['META_ACCESS_TOKEN']}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                flow_id = json.loads(resp.read())["id"]
            print(f"Created flow: {flow_id} (draft)")
        except urllib.error.HTTPError as e:
            print(f"CREATE FAILED HTTP {e.code}: {e.read().decode()}")
            sys.exit(1)

    # 3. Upload screens JSON as the flow_json asset
    raw = SCREENS.read_bytes()
    boundary = "----FareBeepFlowBoundary"
    parts = [
        ("name", "flow.json"),
        ("asset_type", "flow_json"),
        ("file", ("flow.json", raw)),
    ]
    payload = io.BytesIO()
    for name, val in parts:
        payload.write(f"--{boundary}\r\n".encode())
        if isinstance(val, tuple):
            fname, fdata = val
            payload.write(f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                          "Content-Type: application/json\r\n\r\n".encode())
            payload.write(fdata + b"\r\n")
        else:
            payload.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    payload.write(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"https://graph.facebook.com/{ver}/{flow_id}/assets",
        data=payload.getvalue(), method="POST",
        headers={"Authorization": f"Bearer {env['META_ACCESS_TOKEN']}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("Upload result:", json.loads(resp.read()))
    except urllib.error.HTTPError as e:
        print(f"UPLOAD FAILED HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)

    print(f"\nFlow ID: {flow_id}")
    print("Add to FareBeep/.env and Railway:")
    print(f"  BEEP_FLOW_ID={flow_id}")
    print("  BEEP_FLOW_MODE=draft")


if __name__ == "__main__":
    main()
