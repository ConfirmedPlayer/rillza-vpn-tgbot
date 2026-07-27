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

from app.core.settings import get_settings
from app.integrations.celerity import CelerityClient, PanelError

OK = '  OK  '
FAIL = ' FAIL '


async def main() -> int:
    settings = get_settings()
    print(f'Panel: {settings.panel_base_url}')
    failures = 0

    async with CelerityClient(settings) as client:
        try:
            health = await client.health()
            print(f'[{OK}] health: status={health.status!r}')
        except PanelError as error:
            print(f'[{FAIL}] health: {type(error).__name__}: {error}')
            failures += 1

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
                    f'[{FAIL}] group {settings.panel_group_name!r} not found — '
                    'set PANEL_GROUP_NAME to one of the names above'
                )
                failures += 1
        except PanelError as error:
            print(f'[{FAIL}] groups: {type(error).__name__}: {error}')
            failures += 1

    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
