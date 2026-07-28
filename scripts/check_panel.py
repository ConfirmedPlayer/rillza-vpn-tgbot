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

#: Accounts pulled to read group limits off. One page is plenty: every
#: account in a group carries the same expanded group object.
SAMPLE_LIMIT = 50

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
            failures += await _check_device_limit(
                client, settings.panel_group_name
            )
        except PanelError as error:
            report('devices', error)

    return 1 if failures else 0


async def _check_device_limit(client: CelerityClient, group_name: str) -> int:
    """Report the group's configured cap, and who escapes it.

    The group's number is unreadable on its own — the list endpoint
    answers with ids and names only — so it is read off accounts, where
    the panel expands the group they belong to. Reporting one account's
    *resolved* limit is not enough: an account carrying its own
    ``maxDevices`` never consults the group, so a single sample can say
    "no limit" while the group is set correctly. Hence both numbers.
    """
    users, total = await client.iter_users(page=1, limit=SAMPLE_LIMIT)
    if not users:
        print(
            f'[{SKIP}] devices: no accounts on the panel yet. The group '
            'device limit\n'
            '         is only visible through an account that belongs to '
            'it, so run\n'
            '         this again after the first trial or purchase.'
        )
        return 0

    limits = {
        group.name: group.max_devices
        for user in users
        for group in user.groups
    }
    configured = limits.get(group_name)
    if configured is None:
        print(
            f'[{SKIP}] devices: none of the {len(users)} sampled account(s) '
            f'belong to\n'
            f'         {group_name!r}, so its device limit is not '
            'readable yet.'
        )
        return 0

    print(
        f'[{OK}] devices: group {group_name!r} caps at '
        f'{_describe_limit(configured)}'
    )

    overriding = [user for user in users if user.max_devices != 0]
    print(
        f'         {len(users)} of {total} account(s) sampled, '
        f'{len(overriding)} with their own maxDevices'
    )
    if overriding:
        shown = ', '.join(
            f'{user.user_id}({user.max_devices})' for user in overriding[:10]
        )
        print(f'         overriding: {shown}')
        print(
            '         an override wins over the group. Accounts the bot '
            'creates carry\n'
            '         maxDevices=0, so they inherit the group; set an '
            'existing one to 0\n'
            '         to pull it back under the group limit.'
        )
    if configured <= 0:
        print(
            '         note: the group enforces nothing at this value. Set '
            'maxDevices\n'
            '         on the group (panel -> Groups) if you meant to cap '
            'devices.'
        )
    return 0


def _describe_limit(limit: int) -> str:
    if limit > 0:
        return f'{limit} device(s)'
    return 'no device limit'


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
