"""Pins the integration fixture's seed data to the migrations it mirrors.

``tests/integration/conftest.py`` seeds ``SEED_TARIFFS`` by hand because
the integration suite builds its schema with ``Base.metadata.create_all``
— alembic never runs there. That means every title assertion in
``test_buy_flow.py`` and ``test_admin_flow.py`` was only ever checking
the fixture, never the migration that actually ships. A real defect (the
four-device tariffs sharing a title with the two-device ones) survived
nine commits and a whole-branch review for exactly this reason: the
fixture mirrored the migration's mistake, so nothing could see it.

This test recomputes the expected tariff rows straight from the two
migrations' own module data — ``9f9809a15a9c`` (the original seed) and
``c4e1f7a2b930`` (the device-count rename plus the four four-device
rows) — and diffs them against ``SEED_TARIFFS``. Whichever one is edited
next, the other must move with it or this fails.
"""

import importlib.util
from pathlib import Path

from tests.integration.conftest import SEED_TARIFFS

ALEMBIC_VERSIONS = (
    Path(__file__).resolve().parent.parent / 'alembic' / 'versions'
)


def _load_migration(filename: str):
    """Import a migration module straight from its file.

    ``alembic.versions.<rev>`` cannot be used as a dotted import path:
    the project has its own top-level ``alembic/`` directory, but the
    installed ``alembic`` package (the migration tool itself) owns that
    name on ``sys.path``, so the dotted form resolves to site-packages
    and raises ``ModuleNotFoundError`` for anything under it.
    """
    path = ALEMBIC_VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_tariffs_fixture_matches_the_migrations():
    """Replay every tariff migration in order and diff the result.

    Each migration is applied to a dict keyed by code, exactly as the
    real ones apply to rows, so a later one that renames or reprices an
    earlier one's row is accounted for rather than assumed away.
    """
    seed_migration = _load_migration('9f9809a15a9c_seed_tariffs.py')
    device_migration = _load_migration('c4e1f7a2b930_tariff_device_count.py')
    pricing_migration = _load_migration(
        'a7c93e1d4f20_device_sets_and_new_pricing.py'
    )

    # 9f9809a15a9c: the original four, before the column existed.
    expected = {
        code: (title, days, price, 2)
        for code, title, days, price, _order in seed_migration.TARIFFS
    }
    # c4e1f7a2b930: adds the four-device set, renames the two-device
    # titles in place.
    for code, title, days, price, devices, _order in device_migration.TARIFFS:
        expected[code] = (title, days, price, devices)
    for code, _old, new in device_migration.TWO_DEVICE_TITLES:
        _title, days, price, devices = expected[code]
        expected[code] = (new, days, price, devices)
    # a7c93e1d4f20: adds three, six and eight, reprices the rest.
    for code, title, days, price, devices, _order in pricing_migration.TARIFFS:
        expected[code] = (title, days, price, devices)
    for code, _old_price, new_price, _old, _new in pricing_migration.REPRICED:
        title, days, _price, devices = expected[code]
        expected[code] = (title, days, new_price, devices)

    actual = {
        code: (title, days, price, devices)
        for code, title, days, price, devices, _order in SEED_TARIFFS
    }

    assert actual == expected
