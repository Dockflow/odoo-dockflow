"""Client for the Dockflow v3 (Trident) API.

Plain Python with no Odoo import, so it can be tested on its own.
"""

import re
from urllib.parse import quote

import requests

HOSTS = {"prod": "https://api.dockflow.com", "uat": "https://uat.dockflow.com"}
TOKEN_PATTERN = re.compile(r"^(prod|uat)_[a-z0-9-]+_[A-Za-z0-9\-_]{43}$")
USER_AGENT = "dockflow-odoo"
TIMEOUT = 30
# Dockflow accepts at most this many tradeflows per downstream call.
BATCH_SIZE = 100
# A drain stops after this many pages, so a cursor that never ends cannot loop forever.
MAX_PAGES = 50


class DockflowApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def token_environment(token):
    """The environment a token belongs to, or None when it is not a v3 token."""
    match = TOKEN_PATTERN.match(token or "")
    return match.group(1) if match else None


class DockflowClient:
    def __init__(self, token, environment, session=None):
        if environment not in HOSTS:
            raise DockflowApiError(f"Unknown environment {environment!r}.")
        found = token_environment(token)
        if found is None:
            raise DockflowApiError("The API token is not a Dockflow v3 token.")
        if found != environment:
            raise DockflowApiError(
                f"The API token is a {found} token, but the environment is set to {environment}."
            )
        self.token = token
        self.base = HOSTS[environment] + "/v3"
        self.session = session or requests.Session()

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT}
        try:
            return self.session.request(
                method, self.base + path, headers=headers, timeout=TIMEOUT, **kwargs
            )
        except requests.RequestException as exc:
            raise DockflowApiError(f"Dockflow could not be reached: {exc}") from exc

    def check_connection(self):
        """Authenticate without failsafe, so the polling position does not move."""
        response = self._request("GET", "/upstream", params={"tradeflow": "odoo-connection-check", "per_page": 1})
        if response.status_code != 200:
            raise DockflowApiError(_describe(response), response.status_code)

    def send_tradeflows(self, payloads):
        """Create or update tradeflows. Dockflow answers 202 when it accepts them."""
        for start in range(0, len(payloads), BATCH_SIZE):
            batch = payloads[start:start + BATCH_SIZE]
            response = self._request("POST", "/downstream/tradeflows", json=batch)
            if response.status_code != 202:
                raise DockflowApiError(_describe(response), response.status_code)

    def deactivate_tradeflow(self, tradeflow_reference):
        path = "/downstream/tradeflows/" + quote(tradeflow_reference, safe="")
        response = self._request("DELETE", path)
        # 404: the tradeflow is already gone, which is the state we want.
        if response.status_code not in (200, 202, 204, 404):
            raise DockflowApiError(_describe(response), response.status_code)

    def iter_pages(self, since=None, per_page=1000):
        """Pages of records waiting for this key, using failsafe polling.

        Dockflow moves the position on with every call, so the caller must store a
        page before asking for the next one. `since` only seeds the very first call.
        """
        params = {"failsafe": 1, "per_page": per_page}
        if since:
            params["since"] = since
        for _page in range(MAX_PAGES):
            response = self._request("GET", "/upstream", params=dict(params))
            if response.status_code != 200:
                raise DockflowApiError(_describe(response), response.status_code)
            body = response.json()
            yield body.get("data") or []
            cursor = body.get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor


def _describe(response):
    text = (response.text or "").strip().replace("\n", " ")
    return f"Dockflow answered HTTP {response.status_code}: {text[:300]}"
