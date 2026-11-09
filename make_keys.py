"""
Run this ONCE on your own PC:   python make_keys.py

It makes your secrets and prints them as Environment Variables to paste into
Vercel (Settings > Environment Variables). It also saves them in
doorman-secrets.txt so you can copy them later - never upload that file.

For lock-screen alerts it needs:  pip install cryptography
If you run the server yourself instead of Vercel, add --config to also write
config.json next to app.py.
"""
import base64
import json
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "doorman-secrets.txt"


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


if OUT.exists() and input("doorman-secrets.txt exists. Make NEW secrets? (type yes) ").strip().lower() != "yes":
    raise SystemExit("Left it unchanged.")

owner = ""
while len(owner) < 8:
    owner = input("Choose your owner password for the phone (8+ characters): ").strip()

cfg = {
    "APP_KEY": secrets.token_urlsafe(24),
    "OWNER_PASSWORD": owner,
    "SECRET_KEY": secrets.token_hex(32),
}

try:  # optional lock-screen alert keys
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    cfg["VAPID_PRIVATE_KEY"] = b64(priv.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cfg["VAPID_PUBLIC_KEY"] = b64(priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    cfg["VAPID_SUBJECT"] = "mailto:" + (input("Your email (needed for phone alerts): ").strip()
                                        or "owner@example.com")
except ImportError:
    print("\n(Lock-screen alerts skipped: run  pip install cryptography  and this script again to enable them.)")

lines = [f"{k}={v}" for k, v in cfg.items()]
OUT.write_text("\n".join(lines) + "\n")
if "--config" in sys.argv:
    (HERE / "config.json").write_text(json.dumps(cfg, indent=2))
    print("Also wrote config.json")

print("\n=== Add each of these in Vercel > Settings > Environment Variables ===")
for k, v in cfg.items():
    print(f"{k}\n  {v}\n")
print("=== Paste this into the Windows app (Security > Phone approval) ===")
print("App key:", cfg["APP_KEY"])
print(f"\nSaved to {OUT.name}. Keep it private - do not upload it to GitHub.")
