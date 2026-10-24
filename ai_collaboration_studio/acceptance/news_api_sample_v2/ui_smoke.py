"""Optional synthetic-only GUI check. Does not import runner/backend or use a grant."""
import argparse
import json
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from acceptance.news_api_sample_v2.credentials import PromptCancelled, ask_api_key


def no_network(*_, **__):
    raise RuntimeError("synthetic_ui_network_disabled")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-input-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.synthetic_input_only:
        parser.print_help()
        return 0
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    socket.getaddrinfo = no_network
    try:
        text = ask_api_key(expires_at=time.time() + 120, synthetic_only=True)
        count = len(text)
        text = ""
        print(json.dumps({"status": "synthetic_input_accepted", "character_count": count,
                          "model_requests": 0, "grant_used": False}))
        return 0
    except PromptCancelled:
        print(json.dumps({"status": "synthetic_ui_cancelled", "model_requests": 0, "grant_used": False}))
        return 0
    except Exception:
        print(json.dumps({"status": "synthetic_ui_unavailable", "model_requests": 0, "grant_used": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
