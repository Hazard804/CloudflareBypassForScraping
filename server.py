#!/usr/bin/env python3

import argparse
import logging
import os

import uvicorn


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Cloudflare Bypasser Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="Port to bind to (env PORT)")
    parser.add_argument("--workers", type=int, default=1, help="Number of worker processes")
    parser.add_argument("--log-level", type=str, default="info", help="Log level")
    parser.add_argument("--browsers", type=int, default=int(os.environ.get("CF_BROWSERS", "3")),
                        help="Number of reused browsers per worker (default 3)")
    parser.add_argument("--headless", action="store_true", help="Run browsers headless (Cloudflare may detect it)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable cookie caching and mirror session reuse (caching is ON by default; env CF_CACHE_ENABLED)")
    parser.add_argument("--enable-cache", action="store_true",
                        help="Deprecated no-op: caching is on by default. Kept so existing launch scripts keep working")
    parser.add_argument("--auth-token", type=str, default=None,
                        help="Require this value in the x-auth-token header on every request (env CF_AUTH_TOKEN)")

    args = parser.parse_args()
    if args.browsers < 1:
        parser.error("--browsers must be >= 1")

    # workers import the app fresh, so pass settings through the environment
    os.environ["CF_BROWSERS"] = str(args.browsers)
    if args.headless:
        os.environ["CF_HEADLESS"] = "true"
    if args.no_cache:
        os.environ["CF_CACHE_ENABLED"] = "false"
    if args.enable_cache:
        os.environ["CF_CACHE_ENABLED"] = "true"
    if args.auth_token:
        os.environ["CF_AUTH_TOKEN"] = args.auth_token
    
    logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))
    
    logger = logging.getLogger(__name__)
    logger.info(f"Starting server on {args.host}:{args.port} with {args.browsers} browsers per worker")

    # factory import-string so >1 worker can fork; cookie cache is per-worker
    uvicorn.run(
        "cf_bypasser.server.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level=args.log_level
    )


if __name__ == "__main__":
    main()