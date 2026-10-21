# SPDX-License-Identifier: GPL-2.0-or-later
"""Validate every HTTP destination once, returning numeric socket addresses."""
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class UnsafeUrl(ValueError):
    pass


def origin(url):
    parts = urlsplit(url)
    return (parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80))


@dataclass(frozen=True)
class Destination:
    scheme: str
    hostname: str
    port: int
    addresses: tuple


class UrlPolicy:
    """Production rejects non-global addresses; tests may allow exact origins."""
    def __init__(self, test_origins=frozenset(), resolver=None):
        self.test_origins = frozenset(origin(url) for url in test_origins)
        self.resolver = resolver or socket.getaddrinfo

    def resolve(self, url):
        try:
            parts = urlsplit(url)
            if (parts.scheme not in ('http', 'https') or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or any(ord(c) < 33 or ord(c) == 127 for c in url)):
                raise UnsafeUrl('Unsupported or unsafe URL')
            hostname = parts.hostname.encode('idna').decode('ascii')
            port = parts.port or (443 if parts.scheme == 'https' else 80)
            if not 1 <= port <= 65535:
                raise UnsafeUrl('Invalid port')
            allowed = origin(url) in self.test_origins
            records = self.resolver(hostname, port, type=socket.SOCK_STREAM)
            addresses = []
            for family, socktype, proto, _, sockaddr in records:
                address = ipaddress.ip_address(sockaddr[0])
                if not allowed and (not address.is_global or getattr(address, 'ipv4_mapped', None) is not None):
                    raise UnsafeUrl('Non-public destination')
                if family in (socket.AF_INET, socket.AF_INET6):
                    entry = (family, socktype, proto, sockaddr)
                    if entry not in addresses:
                        addresses.append(entry)
            if not addresses:
                raise UnsafeUrl('Destination has no usable addresses')
            return Destination(parts.scheme, hostname, port, tuple(addresses))
        except (ValueError, UnicodeError) as error:
            raise UnsafeUrl('Unsupported or unsafe URL') from error
