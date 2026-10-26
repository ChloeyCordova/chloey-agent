"""Refresh the long-lived Instagram token (they expire after 60 days).

Writes the new token to the file named in NEW_TOKEN_FILE so the workflow can
save it back into the repo's secrets. The token is never printed.
"""
import os

import requests

r = requests.get(
    "https://graph.instagram.com/refresh_access_token",
    params={"grant_type": "ig_refresh_token", "access_token": os.environ["IG_ACCESS_TOKEN"]},
    timeout=60,
)
body = r.json()
if "access_token" not in body:
    raise SystemExit(f"Token refresh failed: {body.get('error', body)}")
token = body["access_token"]
print(f"::add-mask::{token}")
with open(os.environ["NEW_TOKEN_FILE"], "w") as f:
    f.write(token)
print(f"Refreshed. Valid for about {int(body.get('expires_in', 0)) // 86400} days.")
