"""Entry point for qbouncer service."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Config
from .exceptions import ConfigError, QBouncerError
from .killswitch import KillswitchManager
from .service import QBouncerService


def setup_logging(level: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("qbouncer").setLevel(log_level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qbouncer",
        description="WireGuard NAT-PMP Port Bouncer for qBittorrent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        help="Path to configuration file (TOML format)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    parser.add_argument(
        "--remove-killswitch",
        action="store_true",
        help="Remove the killswitch rules and exit (they stay when the service stops)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level from config",
    )
    return parser.parse_args()


def _resolve_log_level(args: argparse.Namespace, config_level: str) -> str:
    """CLI > config > default. --verbose beats everything."""
    if args.verbose:
        return "DEBUG"
    if args.log_level is not None:
        return str(args.log_level)
    return config_level


def _remove_killswitch(config: Config) -> int:
    try:
        KillswitchManager(vpn_interface=config.wg_interface, user=config.killswitch_user).remove()
    except QBouncerError as e:
        logging.getLogger(__name__).error("Failed to remove killswitch: %s", e)
        return 1
    return 0


def main() -> int:
    args = parse_args()

    # Bootstrap logging at INFO so config-load errors are visible before we
    # know the configured level. setup_logging() runs exactly once after
    # config load, below.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger(__name__)

    try:
        config = Config.load(args.config)
    except ConfigError as e:
        logger.error("Configuration error: %s", e)
        return 1

    setup_logging(_resolve_log_level(args, config.log_level))

    if args.remove_killswitch:
        return _remove_killswitch(config)

    logger.info("Starting qbouncer %s", __version__)

    try:
        service = QBouncerService(config)
        service.run()
        return 0
    except QBouncerError as e:
        logger.error("Service error: %s", e)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
