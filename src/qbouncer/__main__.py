"""Entry point for qbouncer service."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Config
from .exceptions import ConfigError, QBouncerError
from .service import QBouncerService


def setup_logging(level: str) -> None:
    """Setup logging configuration.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR)
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    # Set level for our loggers
    logging.getLogger("qbouncer").setLevel(log_level)

    # Reduce noise from requests library
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments namespace
    """
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
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set log level (default: INFO)",
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    args = parse_args()

    # Determine log level
    log_level = "DEBUG" if args.verbose else args.log_level

    # Setup logging early
    setup_logging(log_level)

    logger = logging.getLogger(__name__)
    logger.info("Starting qbouncer %s", __version__)

    try:
        # Load configuration
        config = Config.load(args.config)

        # Override log level from config if not set via CLI
        if not args.verbose and args.log_level == "INFO":
            setup_logging(config.log_level)

        # Create and run service
        service = QBouncerService(config)
        service.run()

        return 0

    except ConfigError as e:
        logger.error("Configuration error: %s", e)
        return 1

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
