"""Every outbound call must resolve names the way the OS does.

``aiohttp[speedups]`` pulls in aiodns, and aiohttp then makes c-ares the
process-wide default resolver — for our panel client, for both payment
providers and for aiogram's session alike, without anyone asking. c-ares
speaks to the nameservers in ``/etc/resolv.conf`` directly: it ignores
``/etc/hosts``, ``nsswitch.conf``, split-DNS pushed by a VPN and the
systemd-resolved stub. On a host where those are the working path,
*everything* dies at once with "Could not contact DNS servers" while
``ping`` and ``curl`` on the same box are fine — a failure mode that
looks like the panel being down.

So the extra stays out of ``pyproject.toml``. This test is the latch: a
transitive dependency that drags aiodns back in flips the default
without touching a line of our code, and that must not pass silently.
"""

from aiohttp.resolver import DefaultResolver, ThreadedResolver


def test_the_os_resolver_is_the_default() -> None:
    assert DefaultResolver is ThreadedResolver, (
        'aiodns is installed again, so aiohttp switched every client in '
        'the process to c-ares — see this module docstring'
    )


def test_aiodns_is_not_installed() -> None:
    """The same check one level down, with a name to grep for."""
    import importlib.util

    assert importlib.util.find_spec('aiodns') is None
