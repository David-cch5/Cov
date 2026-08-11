"""Run the local navigation app.

    python3 scripts/serve.py [--port 8742]

Read-only: the app has no route that writes, so this is safe to leave running
while the pipeline works in another terminal.
"""
import argparse
import sys

sys.path.insert(0, ".")

from app.web.app import app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8742)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    print(f"covenant records -> http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
