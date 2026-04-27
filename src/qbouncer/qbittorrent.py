"""qBittorrent Web API client."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import requests

from .exceptions import QBittorrentError

logger = logging.getLogger(__name__)


class QBittorrentClient:
    """Client for interacting with qBittorrent Web API."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        timeout: int = 10,
        use_https: bool = False,
        verify_ssl: bool = True,
        ca_bundle: str = "",
    ) -> None:
        """Initialize qBittorrent client.

        Args:
            host: qBittorrent host (e.g., localhost)
            port: qBittorrent Web UI port
            username: Optional username for authentication
            password: Optional password for authentication
            timeout: Request timeout in seconds
            use_https: Use HTTPS instead of HTTP
            verify_ssl: Verify SSL certificates (applies when use_https=True and ca_bundle is empty)
            ca_bundle: Path to a PEM bundle for verification (overrides verify_ssl when set)
        """
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"
        self.timeout = timeout
        self.username = username
        self.password = password
        self.use_https = use_https
        # requests accepts either a bool or a path for its verify kwarg.
        self._verify: bool | str = ca_bundle if ca_bundle else verify_ssl
        self.session = requests.Session()
        self._authenticated = False

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid session, authenticate if needed."""
        if not self.username:
            return
        if self._authenticated:
            return
        self._login()

    def _login(self) -> None:
        """Authenticate with the qBittorrent Web API."""
        try:
            response = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
                verify=self._verify,
            )
            response.raise_for_status()

            if response.text == "Ok.":
                logger.info("Authenticated with qBittorrent")
                self._authenticated = True
            else:
                raise QBittorrentError(f"Authentication failed: {response.text}")

        except requests.exceptions.ConnectionError as e:
            raise QBittorrentError(f"Cannot connect to qBittorrent: {e}") from e
        except requests.exceptions.Timeout as e:
            raise QBittorrentError("qBittorrent authentication timed out") from e
        except requests.exceptions.HTTPError as e:
            raise QBittorrentError(f"qBittorrent authentication error: {e}") from e

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Dispatch to session.get / session.post explicitly (typed)."""
        if method == "get":
            return self.session.get(url, **kwargs)
        if method == "post":
            return self.session.post(url, **kwargs)
        raise ValueError(f"Unsupported HTTP method: {method}")

    def _request(
        self,
        method: str,
        endpoint: str,
        _retries: int = 1,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an authenticated request to the qBittorrent API.

        Args:
            method: HTTP method (get, post)
            endpoint: API endpoint path
            _retries: Remaining retries on 403 (internal use)
            **kwargs: Additional arguments to pass to requests

        Raises:
            QBittorrentError: If request fails
        """
        self._ensure_authenticated()
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self._verify)
        url = f"{self.base_url}{endpoint}"

        try:
            response = self._send(method, url, **kwargs)

            if response.status_code == 403 and self.username and _retries > 0:
                logger.debug("Session expired, re-authenticating")
                self._authenticated = False
                return self._request(method, endpoint, _retries=_retries - 1, **kwargs)

            response.raise_for_status()
            return response

        except requests.exceptions.ConnectionError as e:
            raise QBittorrentError(f"Cannot connect to qBittorrent: {e}") from e
        except requests.exceptions.Timeout as e:
            raise QBittorrentError("qBittorrent API request timed out") from e
        except requests.exceptions.HTTPError as e:
            raise QBittorrentError(f"qBittorrent API error: {e}") from e

    def get_preferences(self) -> dict[str, Any]:
        """Get all qBittorrent preferences."""
        try:
            response = self._request("get", "/api/v2/app/preferences")
            return cast(dict[str, Any], response.json())
        except ValueError as e:
            raise QBittorrentError(f"Invalid JSON response: {e}") from e

    def set_preferences(self, preferences: dict[str, Any]) -> None:
        """Set qBittorrent preferences."""
        self._request(
            "post",
            "/api/v2/app/setPreferences",
            data={"json": json.dumps(preferences)},
        )

    def get_listening_port(self) -> int:
        """Get the current listening port."""
        prefs = self.get_preferences()
        port = int(prefs.get("listen_port", 0))
        logger.debug("Current qBittorrent listening port: %d", port)
        return port

    def get_network_interface(self) -> str:
        """Get the configured network interface for binding."""
        prefs = self.get_preferences()
        interface = str(prefs.get("current_network_interface", ""))
        logger.debug("Current qBittorrent network interface: '%s'", interface)
        return interface

    def verify_interface_binding(self, expected: str) -> bool:
        """Verify qBittorrent is bound to the expected interface."""
        current = self.get_network_interface()
        matches = current == expected
        if not matches:
            logger.warning(
                "qBittorrent interface mismatch: expected '%s', got '%s'", expected, current
            )
        return matches

    def update_port_and_interface(self, port: int, interface: str) -> None:
        """Update both listening port and network interface atomically."""
        logger.info("Updating qBittorrent: port=%d, interface='%s'", port, interface)
        self.set_preferences(
            {
                "listen_port": port,
                "current_network_interface": interface,
            }
        )

    def get_version(self) -> str:
        """Get qBittorrent version."""
        try:
            response = self._request("get", "/api/v2/app/version")
            return response.text.strip()
        except QBittorrentError:
            return "unknown"

    def is_reachable(self) -> bool:
        """Check if qBittorrent API is reachable and authenticated."""
        try:
            self._request("get", "/api/v2/app/version", timeout=5)
            return True
        except QBittorrentError:
            return False
