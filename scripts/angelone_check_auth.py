"""
AngelOne Auth Check — run this yourself, not through an AI assistant.
──────────────────────────────────────────────────────────────────────
Verifies that ANGEL_API_KEY / ANGEL_CLIENT_CODE / ANGEL_PASSWORD_OR_PIN /
ANGEL_TOTP_SECRET in your .env actually log in, without ever printing the
secret values themselves — only a CONNECTED/FAILED result and (on success)
your client code and available margin, which are not secrets.

Usage:
  python scripts/angelone_check_auth.py

If this fails, double-check (in your own editor, not by pasting into chat):
  - ANGEL_TOTP_SECRET is the base32 secret from enabling TOTP in the
    AngelOne app (Profile -> Settings -> Enable TOTP), not a 6-digit code
  - ANGEL_CLIENT_CODE matches exactly what AngelOne shows you (case-sensitive)
  - ANGEL_API_KEY is from an app created at https://smartapi.angelbroking.com
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from broker.angelone_adapter import AngelOneAdapter


def main():
    adapter = AngelOneAdapter()
    print("Attempting AngelOne login...")

    if not adapter.authenticate():
        print("\nFAILED — check the logs above for the reason (no secrets are logged).")
        return 1

    print(f"\nCONNECTED — client code: {os.getenv('ANGEL_CLIENT_CODE', '')}")

    margins = adapter.get_margins()
    if margins.get("status"):
        available = margins.get("data", {}).get("net", "unknown")
        print(f"Available margin: {available}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
