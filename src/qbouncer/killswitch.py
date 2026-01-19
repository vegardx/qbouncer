"""iptables killswitch to prevent traffic leaks outside VPN."""

from __future__ import annotations

import logging
import pwd
import subprocess

from .exceptions import KillswitchError

logger = logging.getLogger(__name__)

__all__ = ["KillswitchManager"]


class KillswitchManager:
    """Manage iptables killswitch rules for a specific user.

    Ensures that a user (typically running qBittorrent) can only send
    traffic through the VPN interface, preventing IP leaks if the VPN
    goes down.

    Uses a custom iptables chain for clean management and removal.
    Requires CAP_NET_ADMIN capability.
    """

    CHAIN_NAME = "QBOUNCER-KS"
    TABLE = "filter"

    def __init__(self, vpn_interface: str, user: str) -> None:
        """Initialize killswitch manager.

        Args:
            vpn_interface: VPN interface name (e.g., wg0)
            user: Username to restrict (e.g., qbittorrent)
        """
        self.vpn_interface = vpn_interface
        self.user = user
        self._uid: int | None = None
        self._active = False

    def _get_uid(self) -> int:
        """Get UID for the configured user.

        Returns:
            User ID

        Raises:
            KillswitchError: If user not found
        """
        if self._uid is not None:
            return self._uid

        try:
            self._uid = pwd.getpwnam(self.user).pw_uid
            return self._uid
        except KeyError:
            raise KillswitchError(f"User not found: {self.user}")

    def _run_iptables(
        self,
        args: list[str],
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute iptables command.

        Args:
            args: Command arguments (without 'iptables')
            check: Raise on non-zero exit

        Returns:
            CompletedProcess result

        Raises:
            KillswitchError: If command fails
        """
        cmd = ["iptables"] + args
        logger.debug("Executing: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                check=check,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result
        except subprocess.CalledProcessError as e:
            raise KillswitchError(f"iptables failed: {' '.join(cmd)}\nstderr: {e.stderr}") from e
        except subprocess.TimeoutExpired as e:
            raise KillswitchError(f"iptables timed out: {' '.join(cmd)}") from e
        except FileNotFoundError:
            raise KillswitchError("iptables command not found")

    def _chain_exists(self) -> bool:
        """Check if our custom chain exists."""
        result = self._run_iptables(
            ["-t", self.TABLE, "-n", "-L", self.CHAIN_NAME],
            check=False,
        )
        return result.returncode == 0

    def _rule_exists(self, chain: str, rule_spec: list[str]) -> bool:
        """Check if a rule exists using iptables -C.

        Args:
            chain: Chain name
            rule_spec: Rule specification

        Returns:
            True if rule exists
        """
        result = self._run_iptables(
            ["-t", self.TABLE, "-C", chain] + rule_spec,
            check=False,
        )
        return result.returncode == 0

    def _create_chain(self) -> None:
        """Create the custom chain."""
        if not self._chain_exists():
            logger.info("Creating iptables chain: %s", self.CHAIN_NAME)
            self._run_iptables(["-t", self.TABLE, "-N", self.CHAIN_NAME])

    def _add_jump_rule(self) -> None:
        """Add jump rule from OUTPUT to our chain for the target user."""
        uid = self._get_uid()
        jump_spec = ["-m", "owner", "--uid-owner", str(uid), "-j", self.CHAIN_NAME]

        if not self._rule_exists("OUTPUT", jump_spec):
            logger.info(
                "Adding jump rule: OUTPUT -> %s for user %s (uid %d)",
                self.CHAIN_NAME,
                self.user,
                uid,
            )
            self._run_iptables(["-t", self.TABLE, "-I", "OUTPUT", "1"] + jump_spec)

    def _add_chain_rules(self) -> None:
        """Add the killswitch rules to our chain."""
        rules = [
            # Allow loopback (for Web UI on localhost)
            (["-o", "lo", "-j", "ACCEPT"], "loopback"),
            # Allow established/related (responses to incoming connections)
            (["-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"], "established"),
            # Allow VPN interface
            (["-o", self.vpn_interface, "-j", "ACCEPT"], f"VPN ({self.vpn_interface})"),
            # Reject everything else
            (["-j", "REJECT"], "reject all other"),
        ]

        for rule_spec, description in rules:
            if not self._rule_exists(self.CHAIN_NAME, rule_spec):
                logger.info("Adding killswitch rule: %s", description)
                self._run_iptables(["-t", self.TABLE, "-A", self.CHAIN_NAME] + rule_spec)

    def setup(self) -> None:
        """Set up the killswitch rules.

        Creates the chain, adds the jump rule, and adds all killswitch rules.
        Safe to call multiple times (idempotent).

        Raises:
            KillswitchError: If setup fails
        """
        logger.info(
            "Setting up killswitch for user %s on interface %s",
            self.user,
            self.vpn_interface,
        )

        # Clean up any stale rules from a crash
        if self._chain_exists():
            logger.warning("Found existing killswitch chain, cleaning up first")
            self.cleanup()

        self._create_chain()
        self._add_chain_rules()
        self._add_jump_rule()

        self._active = True
        logger.info("Killswitch active")

    def _flush_chain(self) -> None:
        """Flush all rules from our chain."""
        if self._chain_exists():
            logger.debug("Flushing chain %s", self.CHAIN_NAME)
            self._run_iptables(["-t", self.TABLE, "-F", self.CHAIN_NAME])

    def _remove_jump_rule(self) -> None:
        """Remove jump rule from OUTPUT."""
        uid = self._get_uid()
        jump_spec = ["-m", "owner", "--uid-owner", str(uid), "-j", self.CHAIN_NAME]

        while self._rule_exists("OUTPUT", jump_spec):
            logger.debug("Removing jump rule from OUTPUT")
            self._run_iptables(["-t", self.TABLE, "-D", "OUTPUT"] + jump_spec)

    def _delete_chain(self) -> None:
        """Delete our custom chain."""
        if self._chain_exists():
            logger.debug("Deleting chain %s", self.CHAIN_NAME)
            self._run_iptables(["-t", self.TABLE, "-X", self.CHAIN_NAME])

    def cleanup(self) -> None:
        """Remove all killswitch rules and chain.

        Safe to call multiple times (idempotent).
        Order: flush chain -> remove jump rule -> delete chain
        """
        logger.info("Removing killswitch rules")

        try:
            self._flush_chain()
            self._remove_jump_rule()
            self._delete_chain()
            self._active = False
            logger.info("Killswitch removed")
        except KillswitchError as e:
            logger.error("Failed to cleanup killswitch: %s", e)

    def is_active(self) -> bool:
        """Check if killswitch is active and rules are in place.

        Returns:
            True if killswitch chain exists and has rules
        """
        if not self._chain_exists():
            return False

        # Check that the jump rule exists
        uid = self._get_uid()
        jump_spec = ["-m", "owner", "--uid-owner", str(uid), "-j", self.CHAIN_NAME]
        return self._rule_exists("OUTPUT", jump_spec)

    def verify(self) -> bool:
        """Verify killswitch is functioning correctly.

        Returns:
            True if all rules are in place
        """
        if not self._chain_exists():
            logger.warning("Killswitch chain missing")
            return False

        uid = self._get_uid()
        jump_spec = ["-m", "owner", "--uid-owner", str(uid), "-j", self.CHAIN_NAME]
        if not self._rule_exists("OUTPUT", jump_spec):
            logger.warning("Killswitch jump rule missing")
            return False

        # Verify key rules exist
        if not self._rule_exists(self.CHAIN_NAME, ["-o", self.vpn_interface, "-j", "ACCEPT"]):
            logger.warning("Killswitch VPN rule missing")
            return False

        if not self._rule_exists(self.CHAIN_NAME, ["-j", "REJECT"]):
            logger.warning("Killswitch reject rule missing")
            return False

        return True
