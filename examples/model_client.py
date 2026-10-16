"""Read-only by default. Explicit --submit sends a durable, replayable signal."""
import argparse
from datetime import timedelta
import json
import hashlib
import os
from pathlib import Path
import tempfile

from easytrade.client import Client
from easytrade.domain import utc_now


def persist_signal(outbox, desired):
    """Publish a complete payload atomically; concurrent retries use one expiry."""
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / (hashlib.sha256(desired["signal_id"].encode()).hexdigest() + ".json")
    payload = {**desired, "expires_at": (utc_now() + timedelta(minutes=2)).isoformat()}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=outbox, delete=False) as file:
        temporary = Path(file.name)
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        stored = json.loads(path.read_text(encoding="utf-8"))
        if any(stored[key] != value for key, value in desired.items()):
            raise ValueError("该signal-id已有不同内容，未发送；新意图须使用新编号")
        return stored
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--signal-id")
    parser.add_argument("--symbol")
    parser.add_argument("--side", choices=["buy", "sell"])
    parser.add_argument("--quantity", type=int)
    parser.add_argument("--price")
    args = parser.parse_args()
    root = Path(args.root)
    token = os.environ.get("EASYTRADE_TOKEN") or (root / "runtime/api-token.txt").read_text(encoding="utf-8").strip()
    client = Client(token, args.url)
    health = client.request("/health")
    print(json.dumps({"health": health, "instruments": client.instruments(), "account": client.account()}, ensure_ascii=False, indent=2))
    if not args.submit:
        return
    if not all((args.signal_id, args.symbol, args.side, args.quantity, args.price)):
        parser.error("--submit需要--signal-id --symbol --side --quantity --price")
    # Persist before delivery. A lost response is resolved using this exact ID.
    from easytrade.domain import Signal
    Signal.parse({"signal_id": args.signal_id, "account": health["account"], "symbol": args.symbol,
                  "side": args.side, "quantity": args.quantity, "limit_price": args.price,
                  "expires_at": (utc_now() + timedelta(minutes=2)).isoformat()})
    desired = {"signal_id": args.signal_id, "account": health["account"], "symbol": args.symbol,
               "side": args.side, "quantity": args.quantity, "limit_price": args.price}
    try:
        payload = persist_signal(root / "runtime/model-outbox", desired)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(client.signal(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
