"""
AJ Robotics - Agent Client
HTTP client for calling other machines' REST APIs.
"""

import json
import logging
import os
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

AUTH_TOKEN = os.environ.get("AJ_AGENT_TOKEN", "")


class AgentClient:
    """Lightweight HTTP client for inter-machine API calls (stdlib only)."""

    def __init__(self, host: str, port: int = 5000):
        self.base_url = f"http://{host}:{port}"

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if AUTH_TOKEN:
            h["Authorization"] = f"Bearer {AUTH_TOKEN}"
        return h

    def get(self, path: str, timeout: float = 5) -> tuple[dict | None, str | None]:
        """
        GET request. Returns (data, None) on success or (None, error_msg) on failure.
        """
        url = f"{self.base_url}{path}"
        try:
            req = Request(url, headers=self._headers())
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
                return json.loads(body), None
        except HTTPError as e:
            msg = f"HTTP {e.code} from {url}"
            logger.warning(msg)
            return None, msg
        except (URLError, OSError) as e:
            msg = f"Connection failed: {url} ({e})"
            logger.warning(msg)
            return None, msg
        except json.JSONDecodeError:
            return None, f"Invalid JSON from {url}"

    def post(self, path: str, data: dict | None = None, timeout: float = 10) -> tuple[dict | None, str | None]:
        """
        POST request with JSON body.
        """
        url = f"{self.base_url}{path}"
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        body = json.dumps(data or {}).encode()
        try:
            req = Request(url, data=body, headers=headers, method="POST")
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw), None
        except HTTPError as e:
            msg = f"HTTP {e.code} from {url}"
            logger.warning(msg)
            return None, msg
        except (URLError, OSError) as e:
            msg = f"Connection failed: {url} ({e})"
            logger.warning(msg)
            return None, msg
        except json.JSONDecodeError:
            return None, f"Invalid JSON from {url}"

    def health(self, timeout: float = 3) -> bool:
        """Quick health check — returns True if agent is alive."""
        data, err = self.get("/api/health", timeout=timeout)
        return data is not None and data.get("status") == "ok"
