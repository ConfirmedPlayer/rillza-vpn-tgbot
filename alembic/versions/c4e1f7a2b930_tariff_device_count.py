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
    ('m1x4', '1 месяц', 30, 32_000, 4, 5),
    ('m3x4', '3 месяца', 90, 86_400, 4, 6),
    ('m6x4', '6 месяцев', 180, 153_600, 4, 7),
    ('m12x4', '12 месяцев', 365, 268_800, 4, 8),
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


def downgrade() -> None:
    # Deleting a tariff someone has bought raises on the payments FK.
    # That is the correct outcome: rolling back on a database with
    # sales must fail loudly rather than drop money records.
    codes = ', '.join(f"'{code}'" for code, *_ in TARIFFS)
    op.execute(f'DELETE FROM tariffs WHERE code IN ({codes})')
    op.drop_column('subscriptions', 'max_devices')
    op.drop_column('tariffs', 'max_devices')
