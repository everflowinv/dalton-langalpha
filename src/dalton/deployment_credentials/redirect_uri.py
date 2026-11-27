"""Google OAuth redirect URI eligibility check for deployment settings.

Google OAuth 2.0 Web clients require either an ``https://`` URL with a public
domain suffix (Public Suffix List) or a Google-permitted loopback HTTP address
(``http://localhost[:port]`` or ``http://127.0.0.1[:port]``).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from tld import get_fld

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def google_redirect_uri_eligible(url: str | None) -> tuple[bool, str | None]:
    """Return ``(eligible, reason_code)`` for a public app URL or callback URI.

    Reason codes when ineligible:
    - ``raw_ip_not_allowed``: host is a literal IP address (other than permitted loopback HTTP)
    - ``https_required``: non-loopback address does not use ``https://`` (or URL is empty/invalid)
    - ``private_tld_not_allowed``: host lacks a public top-level suffix (e.g. ``.internal``, ``.local``)
    """
    raw = str(url or "").strip()
    if not raw:
        return False, "https_required"

    try:
        parsed = urlparse(raw)
    except ValueError:
        return False, "https_required"

    scheme = (parsed.scheme or "").strip().lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or scheme not in ("http", "https"):
        return False, "https_required"

    if scheme == "http" and host in _LOOPBACK_HOSTS:
        return True, None

    try:
        ipaddress.ip_address(host)
        return False, "raw_ip_not_allowed"
    except ValueError:
        pass

    if scheme != "https":
        return False, "https_required"

    # Public Suffix List validation: requires a public suffix plus a registered domain label.
    fld = get_fld(f"https://{host}", fix_protocol=False, fail_silently=True)
    if not fld:
        return False, "private_tld_not_allowed"

    return True, None
