"""tariff device count

Adds the device count to tariffs and subscriptions and seeds the four
four-device plans next to the existing ones.

``server_default = '2'`` is the truth for every existing row, not a
guess: the four current tariffs sell two devices, and every live
subscription runs on a panel account with ``maxDevices = 0``, which
inherits the group's limit of two.

Prices are the existing ladder times 1.6. A flat multiplier keeps the
per-month discounts identical in both sets, so the "выгода N%" badges
read the same whichever list the buyer opens.

The four two-device titles are renamed here too, in an ``UPDATE``
rather than a re-seed: they were inserted by ``9f9809a15a9c`` and may
already be live. Both title sets now name their device count — before
this, ``m1`` and ``m1x4`` shared the literal title "1 месяц", and
nothing on the invoice or provider screen told a buyer which one they
were paying for.

Revision ID: c4e1f7a2b930
Revises: dc026fb20f91
Create Date: 2026-08-03 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c4e1f7a2b930'
down_revision: str | Sequence[str] | None = 'dc026fb20f91'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TARIFFS = (
    # code, title, days, price in kopeks, devices, order
    ('m1x4', '1 месяц · до 4 устройств', 30, 32_000, 4, 5),
    ('m3x4', '3 месяца · до 4 устройств', 90, 86_400, 4, 6),
    ('m6x4', '6 месяцев · до 4 устройств', 180, 153_600, 4, 7),
    ('m12x4', '12 месяцев · до 4 устройств', 365, 268_800, 4, 8),
)

#: The two-device tariffs seeded by 9f9809a15a9c, renamed in place so
#: their title also names the device count. ``old`` is what that
#: migration wrote — downgrade() restores exactly that string.
TWO_DEVICE_TITLES = (
    # code, old title, new title
    ('m1', '1 месяц', '1 месяц · до 2 устройств'),
    ('m3', '3 месяца', '3 месяца · до 2 устройств'),
    ('m6', '6 месяцев', '6 месяцев · до 2 устройств'),
    ('m12', '12 месяцев', '12 месяцев · до 2 устройств'),
)


def upgrade() -> None:
    op.add_column(
        'tariffs',
        sa.Column(
            'max_devices', sa.Integer(), nullable=False, server_default='2'
        ),
    )
    op.add_column(
        'subscriptions',
        sa.Column(
            'max_devices', sa.Integer(), nullable=False, server_default='2'
        ),
    )

    tariffs = sa.table(
        'tariffs',
        sa.column('code', sa.String),
        sa.column('title_ru', sa.String),
        sa.column('duration_days', sa.Integer),
        sa.column('price_kopeks', sa.Integer),
        sa.column('max_devices', sa.Integer),
        sa.column('sort_order', sa.Integer),
    )
    op.bulk_insert(
        tariffs,
        [
            {
                'code': code,
                'title_ru': title,
                'duration_days': days,
                'price_kopeks': price,
                'max_devices': devices,
                'sort_order': order,
            }
            for code, title, days, price, devices, order in TARIFFS
        ],
    )

    bind = op.get_bind()
    for code, _old, new in TWO_DEVICE_TITLES:
        bind.execute(
            sa.update(tariffs)
            .where(tariffs.c.code == code)
            .values(title_ru=new)
        )


def downgrade() -> None:
    tariffs = sa.table(
        'tariffs',
        sa.column('code', sa.String),
        sa.column('title_ru', sa.String),
    )
    bind = op.get_bind()
    for code, old, _new in TWO_DEVICE_TITLES:
        bind.execute(
            sa.update(tariffs)
            .where(tariffs.c.code == code)
            .values(title_ru=old)
        )

    # Deleting a tariff someone has bought raises on the payments FK.
    # That is the correct outcome: rolling back on a database with
    # sales must fail loudly rather than drop money records.
    codes = ', '.join(f"'{code}'" for code, *_ in TARIFFS)
    op.execute(f'DELETE FROM tariffs WHERE code IN ({codes})')
    op.drop_column('subscriptions', 'max_devices')
    op.drop_column('tariffs', 'max_devices')
    # The panel is not touched here on purpose, and this downgrade
    # assumes the app code is reverted along with the schema. The old
    # PUT (set_expiry) sends only expireAt, so it cannot clear an
    # explicit maxDevices a bot-managed account already has — those
    # accounts keep their 2 or 4 with no column left to explain it.
    # But the old create_or_get_user sends 'maxDevices': 0 on every
    # create, so any account the panel later loses and the bot
    # recreates (a returning user, a lost row) goes back to 0 — under
    # the group limit again, exactly like before this feature shipped.
