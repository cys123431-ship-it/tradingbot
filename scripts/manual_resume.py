import argparse
from trading_safety.manual_resume import write_manual_resume_request

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    try:
        request = write_manual_resume_request(
            args.symbol,
            args.confirm,
            requested_by="cli",
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Failed to create manual resume request: {exc}") from exc
    print(f"RESUME_REQUESTED {request.request_id} {request.symbol}")

if __name__ == "__main__":
    main()
