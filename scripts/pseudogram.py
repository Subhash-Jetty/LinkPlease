import argparse
import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.getenv("PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com")


def request(method: str, path: str, body: dict | None = None, api_key: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise SystemExit(f"{method} {path} failed with {exc.code}: {detail}") from exc


def cmd_apply(args: argparse.Namespace) -> None:
    body = {
        "name": args.name,
        "email": args.email,
        "phone": args.phone,
        "whatsapp": args.whatsapp or args.phone,
        "linkedin_url": args.linkedin_url,
    }
    print(json.dumps(request("POST", "/v1/apply", body), indent=2))


def cmd_keygen(args: argparse.Namespace) -> None:
    print(json.dumps(request("POST", "/v1/keygen", {"email": args.email}), indent=2))


def cmd_simulate(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.getenv("PSEUDOGRAM_API_KEY")
    if not api_key:
        raise SystemExit("Set PSEUDOGRAM_API_KEY or pass --api-key.")
    body = {
        "webhook_url": args.webhook_url.rstrip("/") + "/webhook",
        "count": args.count,
        "duration_seconds": args.duration_seconds,
    }
    print(json.dumps(request("POST", "/v1/simulate/start", body, api_key), indent=2))


def cmd_truth(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.getenv("PSEUDOGRAM_API_KEY")
    if not api_key:
        raise SystemExit("Set PSEUDOGRAM_API_KEY or pass --api-key.")
    print(json.dumps(request("GET", f"/v1/simulate/{args.run_id}/truth", api_key=api_key), indent=2))


def cmd_submit(args: argparse.Namespace) -> None:
    body = {
        "email": args.email,
        "github_repo": args.github_repo,
        "working_url": args.working_url.rstrip("/"),
        "loom_url": args.loom_url,
        "parts_completed": args.parts_completed,
        "start_date": args.start_date,
    }
    print(json.dumps(request("POST", "/v1/submit", body), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="PseudoGram assignment helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--name", required=True)
    apply_cmd.add_argument("--email", required=True)
    apply_cmd.add_argument("--phone", required=True)
    apply_cmd.add_argument("--whatsapp")
    apply_cmd.add_argument("--linkedin-url", required=True)
    apply_cmd.set_defaults(func=cmd_apply)

    keygen_cmd = sub.add_parser("keygen")
    keygen_cmd.add_argument("--email", required=True)
    keygen_cmd.set_defaults(func=cmd_keygen)

    simulate_cmd = sub.add_parser("simulate")
    simulate_cmd.add_argument("--webhook-url", required=True)
    simulate_cmd.add_argument("--count", type=int, default=500)
    simulate_cmd.add_argument("--duration-seconds", type=int, default=10)
    simulate_cmd.add_argument("--api-key")
    simulate_cmd.set_defaults(func=cmd_simulate)

    truth_cmd = sub.add_parser("truth")
    truth_cmd.add_argument("--run-id", required=True)
    truth_cmd.add_argument("--api-key")
    truth_cmd.set_defaults(func=cmd_truth)

    submit_cmd = sub.add_parser("submit")
    submit_cmd.add_argument("--email", required=True)
    submit_cmd.add_argument("--github-repo", required=True)
    submit_cmd.add_argument("--working-url", required=True)
    submit_cmd.add_argument("--loom-url", required=True)
    submit_cmd.add_argument("--parts-completed", default="A+B+C", choices=["A", "A+B", "A+B+C"])
    submit_cmd.add_argument("--start-date", required=True)
    submit_cmd.set_defaults(func=cmd_submit)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

