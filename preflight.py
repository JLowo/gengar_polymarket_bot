#!/usr/bin/env python3
"""Pre-flight check before live trading. Places no orders, sets no approvals,
moves no money, and never prints the private key or builder secrets.

    .venv/bin/python preflight.py                       # full check through Tor
    .venv/bin/python preflight.py --no-tor              # skip Tor (diagnosis only)
    .venv/bin/python preflight.py --create-builder-key  # also create the builder
                                                        # API key used to redeem
                                                        # winnings, saved to .env

Checks: .env settings, Tor, CLOB reachability, that SAFE_ADDRESS is this key's
Polymarket Safe (GNOSIS_SAFE), CLOB V2 trading approvals, pUSD balance,
redeemable winnings, and whether winnings can be redeemed automatically.
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
sys.path.insert(0, HERE)

OK, BAD, NOTE = "✅", "❌", "ℹ️ "
problems = []


def check(ok: bool, good: str, bad: str):
    print(f"  {OK if ok else BAD} {good if ok else bad}")
    if not ok:
        problems.append(bad)
    return ok


def save_builder_key(key) -> None:
    """Append the builder key to .env without displaying it."""
    with open(ENV_PATH, "a") as f:
        f.write("\n# Builder API key for redeeming winnings (created by preflight.py)\n")
        f.write(f"BUILDER_API_KEY={key.key}\n")
        f.write(f"BUILDER_API_SECRET={key.secret}\n")
        f.write(f"BUILDER_API_PASSPHRASE={key.passphrase}\n")
    os.chmod(ENV_PATH, 0o600)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-tor", action="store_true", help="skip Tor (diagnosis only)")
    ap.add_argument("--create-builder-key", action="store_true",
                    help="create a builder API key for redeeming and save it to .env")
    args = ap.parse_args()

    print("\nGengar pre-flight check (no orders, no approvals, no money moved)\n")

    # 1. Settings ---------------------------------------------------------
    print("Settings (.env)")
    if not check(os.path.exists(ENV_PATH), ".env found", ".env missing (cp .env.example .env)"):
        return finish(None)
    load_dotenv(ENV_PATH)
    pk, safe = os.getenv("PRIVATE_KEY", ""), os.getenv("SAFE_ADDRESS", "")
    check(bool(pk), "PRIVATE_KEY is set", "PRIVATE_KEY is empty")
    check(bool(safe), f"SAFE_ADDRESS is set ({safe[:6]}…{safe[-4:]})" if safe else "",
          "SAFE_ADDRESS is empty")
    dry = os.getenv("DRY_RUN", "true")
    print(f"  {NOTE} DRY_RUN={dry!r} ({'dry run' if dry.lower() == 'true' else 'LIVE TRADING'})")
    print(f"  {NOTE} MAX_BET={os.getenv('MAX_BET', '25.0 (default)')} | "
          f"DAILY_LOSS_LIMIT={os.getenv('DAILY_LOSS_LIMIT', '30.0 (default)')} | "
          f"BANKROLL={os.getenv('BANKROLL', '100.0 (default)')} (live uses the real balance)")
    builder_parts = [os.getenv(k, "") for k in
                     ("BUILDER_API_KEY", "BUILDER_API_SECRET", "BUILDER_API_PASSPHRASE")]
    has_builder = all(builder_parts)
    if any(builder_parts) and not has_builder:
        check(False, "", "BUILDER_API_* is only partly set in .env")
    if not (pk and safe):
        return finish(None)

    # 2. Tor ----------------------------------------------------------------
    print("\nNetwork")
    import proxy
    if args.no_tor:
        print(f"  {NOTE} Tor skipped (--no-tor): order placement would not be routed through it")
    else:
        try:
            url = proxy.ensure_tor()
            proxy.apply_proxy(url)
            check(proxy.check_proxy(url), "Tor is up and traffic exits through it",
                  "Tor started but no exit IP could be confirmed")
        except Exception as e:
            check(False, "", f"Tor failed to start: {e}")
            return finish(proxy)

    from executor import Executor
    ex = Executor(private_key=pk, safe_address=safe, dry_run=False)
    try:
        ex.health_check()
        check(True, "CLOB API answers", "")
    except Exception as e:
        check(False, "", f"CLOB API not reachable: {type(e).__name__}: {e}")
        return finish(proxy)

    # 3. Account (the same checks the bot runs before live trading) ----------
    print("\nAccount")
    from polymarket.auth import BuilderApiKey
    ex.builder_key = BuilderApiKey(*builder_parts) if has_builder else None
    if not check(ex.connect(), "Connected to Polymarket as this wallet",
                 "Could not connect as this wallet (see the message above)"):
        return finish(proxy)
    try:
        st = ex.account_status()
        if not check(st.is_safe,
                     "SAFE_ADDRESS is this key's Polymarket Safe (GNOSIS_SAFE)",
                     f"SDK sees SAFE_ADDRESS as {st.wallet_type}, not GNOSIS_SAFE: "
                     f"wrong address for this key?"):
            return finish(proxy)
        check(st.approved, "CLOB V2 trading approvals are in place",
              f"Trading approvals missing: {st.missing or st.error} "
              f"(enable trading in the Polymarket UI)")
        if st.optional_missing:
            print(f"  {NOTE} {st.optional_missing} approval(s) for Polymarket perps / auto-redeem "
                  f"not granted: not needed by the bot, and safer left off")
        min_bet = float(os.getenv("MIN_BET", "5.0"))
        if st.balance is None:
            check(False, "", st.error)
        else:
            check(st.balance >= min_bet, f"pUSD balance: ${st.balance:.2f}",
                  f"pUSD balance is ${st.balance:.2f} (below MIN_BET ${min_bet:.2f}): "
                  f"deposit or convert USDC.e to pUSD in the Polymarket UI")

        ex._initialized = True
        winnings = ex.redeemable_winnings()
        total = sum(v for _, v in winnings)
        print(f"  {NOTE} Redeemable winnings: ${total:.2f} in {len(winnings)} market(s)")

        # 4. Redeeming --------------------------------------------------------
        print("\nRedeeming winnings")
        if has_builder:
            print(f"  {OK} Builder API key configured: the bot will redeem winnings itself")
        elif args.create_builder_key:
            key = ex.client.create_builder_api_key()
            save_builder_key(key)
            print(f"  {OK} Builder API key created and saved to .env (not shown). "
                  f"Run preflight again to confirm it loads.")
        else:
            print(f"  {NOTE} No builder API key: winnings will need claiming in the Polymarket UI.")
            print(f"     To let the bot redeem them, add BUILDER_API_KEY/SECRET/PASSPHRASE to .env,")
            print(f"     or create one with: .venv/bin/python preflight.py --create-builder-key")
    finally:
        ex.close()
    return finish(proxy)


def finish(proxy_module) -> int:
    if proxy_module is not None:
        try:
            proxy_module._stop_tor()
        except Exception:
            pass
    print()
    if problems:
        print(f"NOT READY for live trading ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("READY for live trading, as far as these checks go.")
    print("Before starting: make sure no other copy of the bot is running with this wallet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
