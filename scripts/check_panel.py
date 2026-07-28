"""Read-only check of the CELERITY panel configuration.

Verifies that the API key works and that ``PANEL_GROUP_NAME`` matches a
real server group, using the bot's own client. Makes only GET requests:
nothing is created, changed or deleted.

    uv run python -m scripts.check_panel

In a deployed container:

    docker compose exec bot python -m scripts.check_panel
"""

import asyncio
import sys
from urllib.parse import urlsplit

from app.core.settings import get_settings
from app.integrations.celerity import (
    CelerityClient,
    PanelAuthError,
    PanelError,
    PanelForbiddenError,
)

OK = '  OK  '
FAIL = ' FAIL '
SKIP = ' SKIP '

#: Substrings of an aiohttp error that mean "the name never resolved",
#: as opposed to "we reached the host and it said no".
DNS_MARKERS = ('DNS', 'getaddrinfo', 'Name or service not known')


def _hint(error: PanelError, host: str) -> str | None:
    """Turn a transport failure into the next thing to try."""
    text = str(error)
    if any(marker in text for marker in DNS_MARKERS):
        return (
            f'the hostname {host!r} did not resolve. Check it from the '
            'same machine:\n'
            f'    getent hosts {host}\n'
            'If that works while this does not, something reinstalled '
            'aiodns — see tests/test_dns_resolver.py.'
        )
    if isinstance(error, PanelAuthError | PanelForbiddenError):
        return (
            'the panel rejected the key: check PANEL_API_KEY, and that '
            'nothing (a proxy, Cloudflare) answers before the panel does.'
        )
    if 'Cannot connect' in text or 'timeout' in text:
        return (
            f'{host} resolved but refused or dropped the connection — '
            'panel down, or a firewall in between.'
        )
    return None


async def main() -> int:
    settings = get_settings()
    host = urlsplit(settings.panel_base_url).hostname or ''
    print(f'Panel: {settings.panel_base_url}')
    failures = 0

    def report(label: str, error: PanelError) -> None:
        nonlocal failures
        print(f'[{FAIL}] {label}: {type(error).__name__}: {error}')
        hint = _hint(error, host)
        if hint:
            indent = '         '
            body = hint.replace('\n', f'\n{indent}   ')
            print(f'{indent}-> {body}')
        failures += 1

    async with CelerityClient(settings) as client:
        try:
            health = await client.health()
            print(f'[{OK}] health: status={health.status!r}')
        except PanelError as error:
            report('health', error)

        try:
            groups = await client.list_groups()
            print(f'[{OK}] api key accepted, {len(groups)} active group(s):')
            for group in groups:
                marker = (
                    ' <- PANEL_GROUP_NAME'
                    if group.name == settings.panel_group_name
                    else ''
                )
                print(f'         {group.name}{marker}')

            if any(g.name == settings.panel_group_name for g in groups):
                print(f'[{OK}] group {settings.panel_group_name!r} resolved')
            else:
                print(
                    f'[{FAIL}] group {settings.panel_group_name!r} '
                    'not found — set PANEL_GROUP_NAME to a name above'
                )
                failures += 1
        except PanelError as error:
            report('groups', error)

        try:
            failures += await _check_device_limit(client)
        except PanelError as error:
            report('devices', error)

    return 1 if failures else 0


async def _check_device_limit(client: CelerityClient) -> int:
    """Report the cap the panel would enforce on a real account.

    The group's own number is unreadable on its own — the list endpoint
    answers with ids and names only — so this reads it off an existing
    account, where the panel expands the group it belongs to. Which
    means it says nothing until the first account exists.
    """
    users, total = await client.iter_users(page=1, limit=1)
    if not users:
        print(
            f'[{SKIP}] devices: no accounts on the panel yet. The group '
            'device limit\n'
            '         is only visible through an account that belongs to '
            'it, so run\n'
            '         this again after the first trial or purchase.'
        )
        return 0

    user = users[0]
    limit = user.effective_device_limit
    names = ', '.join(group.name for group in user.groups) or '—'
    print(
        f'[{OK}] devices: account {user.user_id} (of {total}) '
        f'resolves to {_describe_limit(limit)}'
    )
    print(f'         own maxDevices={user.max_devices}, groups: {names}')
    if limit <= 0:
        print(
            '         note: nothing is enforced at this value. Set '
            'maxDevices on the\n'
            '         group (panel -> Groups) if you meant to cap devices.'
        )
    return 0


def _describe_limit(limit: int) -> str:
    if limit > 0:
        return f'{limit} device(s)'
    return 'no device limit'


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
