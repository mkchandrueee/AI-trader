"""
mStock Auth Check — run this yourself, not through an AI assistant.
──────────────────────────────────────────────────────────────────────
Verifies that MSTOCK_API_KEY / MSTOCK_USER_ID / MSTOCK_PASSWORD /
MSTOCK_TOTP_SECRET in your .env actually log in, without ever printing the
secret values themselves — only a CONNECTED/FAILED result and (on success)
your user ID and available funds, which are not secrets.

Usage:
  python scripts/mstock_check_auth.py

If this fails, double-check (in your own editor, not by pasting into chat):
  - MSTOCK_TOTP_SECRET is the base32 secret from enabling TOTP at
    trade.mstock.com (Hamburger Menu -> Key Products -> Trading APIs ->
    Enable TOTP), not a 6-digit code
  - MSTOCK_USER_ID matches exactly what mStock shows you (case-sensitive)
  - MSTOCK_API_KEY is from an app registered at https://trade.mstock.com
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from broker.mstock_adapter import MStockAdapter


def main():
    adapter = MStockAdapter()
    print("Attempting mStock login...")

    if not adapter.authenticate():
        print(f"\nFAILED — {adapter.last_error}")
        return 1

    print(f"\nCONNECTED — user ID: {os.getenv('MSTOCK_USER_ID', '')}")

    margins = adapter.get_margins()
    if margins:
        print(f"Fund summary: {margins.get('data', margins)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
