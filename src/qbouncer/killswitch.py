"""iptables killswitch to prevent traffic leaks outside VPN.

Applies rules to both iptables (IPv4) and ip6tables (IPv6) when IPv6 is enabled
on the host, so traffic for the configured user can only leave via the VPN
interface. The chain is rebuilt in place in one iptables-restore transaction,
so there is never a moment without rules. The rules stay when qbouncer stops;
only remove() takes them away.
"""

from __future__ import annotations

import logging
import pwd
import subprocess
from pathlib import Path

from .exceptions import KillswitchError

logger = logging.getLogger(__name__)

__all__ = ["KillswitchManager"]

IPV6_DISABLE_FLAG = Path("/proc/sys/net/ipv6/conf/all/disable_ipv6")


class KillswitchManager:
    """Manage iptables/ip6tables killswitch rules for a specific user.

    Ensures that a user (typically running qBittorrent) can only send traffic
    through the VPN interface, preventing IP leaks if the VPN goes down.

    Uses a custom chain for clean management and removal. Requires CAP_NET_ADMIN.
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
        self._ipv6_enabled = self._detect_ipv6()

    @staticmethod
    def _detect_ipv6() -> bool:
        """Check if IPv6 is enabled on the host."""
        try:
            value = IPV6_DISABLE_FLAG.read_text().strip()
            return value == "0"
        except OSError:
            return False

    def _get_uid(self) -> int:
        """Get UID for the configured user."""
        if self._uid is not None:
            return self._uid
        try:
            self._uid = pwd.getpwnam(self.user).pw_uid
            return self._uid
        except KeyError as e:
            raise KillswitchError(f"User not found: {self.user}") from e

    def _commands(self) -> list[tuple[str, str]]:
        """Return (binary, restore-binary) pairs for every enabled family."""
        pairs = [("iptables", "iptables-restore")]
        if self._ipv6_enabled:
            pairs.append(("ip6tables", "ip6tables-restore"))
        return pairs

    def _run(self, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Execute an iptables-family command."""
        logger.debug("Executing: %s", " ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                check=check,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as e:
            raise KillswitchError(f"{cmd[0]} failed: {' '.join(cmd)}\nstderr: {e.stderr}") from e
        except subprocess.TimeoutExpired as e:
            raise KillswitchError(f"{cmd[0]} timed out: {' '.join(cmd)}") from e
        except FileNotFoundError as e:
            raise KillswitchError(f"{cmd[0]} command not found") from e

    def _chain_exists(self, binary: str) -> bool:
        """Check if our custom chain exists for a given iptables family."""
        result = self._run(
            [binary, "-t", self.TABLE, "-n", "-L", self.CHAIN_NAME],
            check=False,
        )
        return result.returncode == 0

    def _jump_spec(self, action: str) -> list[str]:
        return [
            "-t",
            self.TABLE,
            action,
            "OUTPUT",
            "-m",
            "owner",
            "--uid-owner",
            str(self._get_uid()),
            "-j",
            self.CHAIN_NAME,
        ]

    def _jump_exists(self, binary: str) -> bool:
        return self._run([binary, *self._jump_spec("-C")], check=False).returncode == 0

    def _wg_show(self, field: str) -> str:
        result = self._run(["wg", "show", self.vpn_interface, field], check=False)
        if result.returncode != 0:
            raise KillswitchError(
                f"'wg show {self.vpn_interface} {field}' failed: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _tunnel_rules(self, binary: str) -> list[str]:
        """Match fragments for the tunnel's own encrypted packets.

        WireGuard's outer UDP packets can keep the owner of the socket that
        sent the inner packet, so they reach this chain on the physical
        interface. Without an exemption the killswitch blocks the tunnel
        itself. wg-quick marks those packets with the interface's fwmark;
        without one, allow UDP to the peer endpoints.
        """
        fwmark = self._wg_show("fwmark")
        if fwmark and fwmark != "off":
            return [f"-m mark --mark {fwmark}"]

        want_v6 = binary == "ip6tables"
        rules = []
        for line in self._wg_show("endpoints").splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[1] == "(none)":
                continue
            host, _, port = parts[1].rpartition(":")
            if host.startswith("[") != want_v6:
                continue
            rules.append(f"-p udp -d {host.strip('[]')} --dport {port}")
        return rules

    def _render_ruleset(self, add_jump: bool, tunnel_rules: list[str]) -> str:
        """Build an iptables-save style ruleset for atomic restore.

        The chain is declared and flushed, then refilled, in one transaction:
        an existing jump keeps pointing at a complete chain throughout. Order
        matters: INVALID first, loopback, the VPN interface, the tunnel's own
        packets, then REJECT.
        """
        chain = self.CHAIN_NAME
        lines = [
            "*filter",
            f":{chain} - [0:0]",
            f"-F {chain}",
            f"-A {chain} -m conntrack --ctstate INVALID -j DROP",
            f"-A {chain} -o lo -j ACCEPT",
            f"-A {chain} -o {self.vpn_interface} -j ACCEPT",
            *(f"-A {chain} {rule} -j ACCEPT" for rule in tunnel_rules),
            f"-A {chain} -j REJECT",
        ]
        if add_jump:
            lines.append(f"-I OUTPUT -m owner --uid-owner {self._get_uid()} -j {chain}")
        lines += ["COMMIT", ""]
        return "\n".join(lines)

    def _apply_ruleset(self, binary: str, restore_binary: str) -> None:
        """Rebuild the chain in place, adding the OUTPUT jump only if missing."""
        add_jump = not (self._chain_exists(binary) and self._jump_exists(binary))
        ruleset = self._render_ruleset(add_jump, self._tunnel_rules(binary))
        logger.debug("Applying %s ruleset:\n%s", restore_binary, ruleset)
        try:
            completed = subprocess.run(
                [restore_binary, "--noflush"],
                input=ruleset,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            logger.debug("%s output: %s", restore_binary, completed.stdout)
        except subprocess.CalledProcessError as e:
            raise KillswitchError(
                f"{restore_binary} failed:\n{e.stderr}\nruleset:\n{ruleset}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise KillswitchError(f"{restore_binary} timed out") from e
        except FileNotFoundError as e:
            raise KillswitchError(f"{restore_binary} command not found") from e

    def _teardown_family(self, binary: str) -> None:
        """Remove the jump rule(s), then flush and delete the chain. Idempotent."""
        for _ in range(16):  # defensively handle administrator-added duplicates
            result = self._run([binary, *self._jump_spec("-D")], check=False)
            if result.returncode != 0:
                break

        if self._chain_exists(binary):
            self._run([binary, "-t", self.TABLE, "-F", self.CHAIN_NAME], check=False)
            self._run([binary, "-t", self.TABLE, "-X", self.CHAIN_NAME], check=False)

    def setup(self) -> None:
        """Install killswitch rules for all enabled address families.

        Safe to call multiple times. Applied atomically per-family via
        iptables-restore. Fails closed: if IPv6 is enabled but ip6tables is not
        installed, raises KillswitchError rather than silently leaking v6.
        """
        logger.info(
            "Setting up killswitch for user %s on interface %s (ipv6=%s)",
            self.user,
            self.vpn_interface,
            self._ipv6_enabled,
        )
        for binary, restore_binary in self._commands():
            self._apply_ruleset(binary, restore_binary)
        logger.info("Killswitch active")

    def remove(self) -> None:
        """Remove all killswitch rules and chains. Safe to call multiple times.

        Not called on shutdown: the rules stay so the user's traffic keeps
        failing closed while qbouncer is stopped. Used by --remove-killswitch.
        """
        logger.info("Removing killswitch rules")
        for binary, _ in self._commands():
            try:
                self._teardown_family(binary)
            except KillswitchError as e:
                logger.error("Failed to remove %s killswitch: %s", binary, e)
        logger.info("Killswitch removed")

    def verify(self) -> bool:
        """Verify killswitch rules are in place for every enabled family."""
        for binary, _ in self._commands():
            if not self._chain_exists(binary):
                logger.warning("%s killswitch chain missing", binary)
                return False
            if not self._jump_exists(binary):
                logger.warning("%s killswitch jump rule missing", binary)
                return False
            vpn_rule = [
                "-t",
                self.TABLE,
                "-C",
                self.CHAIN_NAME,
                "-o",
                self.vpn_interface,
                "-j",
                "ACCEPT",
            ]
            if self._run([binary, *vpn_rule], check=False).returncode != 0:
                logger.warning("%s killswitch VPN rule missing", binary)
                return False
            for rule in self._tunnel_rules(binary):
                spec = [
                    binary,
                    "-t",
                    self.TABLE,
                    "-C",
                    self.CHAIN_NAME,
                    *rule.split(),
                    "-j",
                    "ACCEPT",
                ]
                if self._run(spec, check=False).returncode != 0:
                    logger.warning("%s killswitch tunnel rule missing: %s", binary, rule)
                    return False
            reject_rule = [
                "-t",
                self.TABLE,
                "-C",
                self.CHAIN_NAME,
                "-j",
                "REJECT",
            ]
            if self._run([binary, *reject_rule], check=False).returncode != 0:
                logger.warning("%s killswitch reject rule missing", binary)
                return False
        return True
