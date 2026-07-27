"""seed tariffs

Starting prices from PLAN.md §3. They are editable from the admin panel
afterwards, so this only fills an empty table and never overwrites a
price the operator has changed.

Revision ID: 9f9809a15a9c
Revises: b9a904907743
Create Date: 2026-07-27 08:41:13.344860

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '9f9809a15a9c'
down_revision: str | Sequence[str] | None = 'b9a904907743'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TARIFFS = (
    # code, title, days, price in kopeks, order
    ('m1', '1 месяц', 30, 20_000, 1),
    ('m3', '3 месяца', 90, 54_000, 2),
    ('m6', '6 месяцев', 180, 96_000, 3),
    ('m12', '12 месяцев', 365, 168_000, 4),
)


def upgrade() -> None:
    tariffs = sa.table(
        'tariffs',
        sa.column('code', sa.String),
        sa.column('title_ru', sa.String),
        sa.column('duration_days', sa.Integer),
        sa.column('price_kopeks', sa.Integer),
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
                'sort_order': order,
            }
            for code, title, days, price, order in TARIFFS
        ],
    )


def downgrade() -> None:
    codes = ', '.join(f"'{code}'" for code, *_ in TARIFFS)
    op.execute(f'DELETE FROM tariffs WHERE code IN ({codes})')
