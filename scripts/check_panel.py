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
from collections import Counter
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

#: The panel's own reserved meanings: -1 = unlimited, 0 = inherit the
#: group's limit. Anything below -1 is not a state the panel should
#: ever report, so that is the only value shape this script fails on.
#: A positive count is whatever the operator currently sells — the bot
#: reads those straight from the tariffs table (``buy.py`` calls
#: ``uow.tariffs.list_device_counts()``), so this script must not
#: hardcode which positive numbers are "expected": a new N-device
#: tariff is a migration and nothing else, and should not turn this
#: check red.
MIN_VALID_LIMIT = -1


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
    """Report the group's configured cap, and who does not consult it.

    The group's number is unreadable on its own — the list endpoint
    answers with ids and names only — so it is read off accounts, where
    the panel expands the group they belong to. Reporting one account's
    *resolved* limit is not enough: an account carrying its own
    ``maxDevices`` never consults the group, so a single sample can say
    "no limit" while the group is set correctly. Hence both numbers.

    An explicit ``maxDevices`` is the normal state for a bot-managed
    account, not a red flag — the bot sets a positive count on every
    one it provisions, and that count comes from the tariffs table, not
    from anything this script knows in advance. The only anomaly this
    script can spot without a database is a value the panel's own
    semantics rule out entirely (below -1). Whether one particular
    account's number is the *right* one for what its owner paid is a
    per-account comparison against the database — that is the
    reconciler's job (``devices_fixed`` in its report), not this
    script's.
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

    seen = Counter(user.max_devices for user in users)
    breakdown = ', '.join(
        f'{value}={count}' for value, count in sorted(seen.items())
    )
    print(
        f'         {len(users)} of {total} account(s) sampled, '
        f'maxDevices seen: {breakdown}\n'
        '         An explicit value is expected, not an anomaly: the '
        'bot sets maxDevices\n'
        '         on every account it manages from the tariffs table '
        '(buy.py reads it\n'
        '         via list_device_counts()), so a new N-device tariff '
        'is meant to add\n'
        '         a new number to the line above — this script does '
        'not keep its own\n'
        '         list of "expected" counts to compare against. The '
        'group limit is\n'
        '         only the fallback, for hand-made accounts and rows '
        'the reconciler\n'
        '         has not caught up with yet. This script has no '
        'database access, so\n'
        "         it cannot tell whether one account's number is what "
        'its owner\n'
        '         actually paid for; that comparison runs continuously '
        'in the\n'
        '         reconciler (a mismatch shows up as devices_fixed in '
        'its report).'
    )

    invalid = [u for u in users if u.max_devices < MIN_VALID_LIMIT]
    if invalid:
        shown = ', '.join(
            f'{user.user_id}({user.max_devices})' for user in invalid[:10]
        )
        print(
            f'[{FAIL}] devices: {len(invalid)} account(s) carry a '
            f'maxDevices below {MIN_VALID_LIMIT} —\n'
            '         not a state the panel should ever be in (-1 = '
            'unlimited, 0 =\n'
            f'         inherit the group, otherwise a positive count): '
            f'{shown}'
        )
        return 1

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
