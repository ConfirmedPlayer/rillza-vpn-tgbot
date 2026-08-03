"""device sets and new pricing

Replaces the two-tier price list with one computed from a rate:

    price = (100 + (devices - 2) * 50) * months * (1 - duration discount)

Three consequences worth naming, because none of them is reversible by
editing a number afterwards.

*The base rate halves.* A two-device month was 200 ₽ and becomes 100 ₽;
every row below it moves with it. This is a deliberate repricing of the
whole catalogue, decided by the owner after seeing that the new
four-device column lands exactly on the old two-device one.

*This migration overwrites prices the operator may have edited.*
``9f9809a15a9c`` went out of its way not to — it only filled an empty
table. That was right for a seed and is wrong here: the point of this
change is that every price comes from one formula, so a cell left at its
old value would be the bug, not the fix. Prices stay editable from the
admin screen afterwards, as before.

*``m3x3`` is 405 ₽, not a round 400.* Rounding it would make the
three-device set advertise "выгода 11%" while every other set says 10%,
because ``keyboards.tariffs`` derives the badge from the per-month price
within the list shown. A flat rate is what keeps the ladder identical
across sets, and 405 is what a flat rate gives.

Sort order is regrouped so the admin screen lists whole device sets
together: 2 → 1-4, 3 → 5-8, 4 → 9-12, 6 → 13-16, 8 → 17-20. Inside one
set the order is unchanged, and the purchase screen filters by device
count anyway, so only the admin list looks different.

Revision ID: a7c93e1d4f20
Revises: c4e1f7a2b930
Create Date: 2026-08-03 13:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a7c93e1d4f20'
down_revision: str | Sequence[str] | None = 'c4e1f7a2b930'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The three-, six- and eight-device sets. Two and four already exist
#: and are repriced below rather than re-seeded.
TARIFFS = (
    # code, title, days, price in kopeks, devices, order
    ('m1x3', '1 месяц · до 3 устройств', 30, 15_000, 3, 5),
    ('m3x3', '3 месяца · до 3 устройств', 90, 40_500, 3, 6),
    ('m6x3', '6 месяцев · до 3 устройств', 180, 72_000, 3, 7),
    ('m12x3', '12 месяцев · до 3 устройств', 365, 126_000, 3, 8),
    ('m1x6', '1 месяц · до 6 устройств', 30, 30_000, 6, 13),
    ('m3x6', '3 месяца · до 6 устройств', 90, 81_000, 6, 14),
    ('m6x6', '6 месяцев · до 6 устройств', 180, 144_000, 6, 15),
    ('m12x6', '12 месяцев · до 6 устройств', 365, 252_000, 6, 16),
    ('m1x8', '1 месяц · до 8 устройств', 30, 40_000, 8, 17),
    ('m3x8', '3 месяца · до 8 устройств', 90, 108_000, 8, 18),
    ('m6x8', '6 месяцев · до 8 устройств', 180, 192_000, 8, 19),
    ('m12x8', '12 месяцев · до 8 устройств', 365, 336_000, 8, 20),
)

#: The rows that already exist, moved onto the new formula. ``old`` is
#: what the earlier migrations wrote, so downgrade() restores exactly
#: that — including the sort order the four-device set used to have.
REPRICED = (
    # code, old price, new price, old order, new order
    ('m1', 20_000, 10_000, 1, 1),
    ('m3', 54_000, 27_000, 2, 2),
    ('m6', 96_000, 48_000, 3, 3),
    ('m12', 168_000, 84_000, 4, 4),
    ('m1x4', 32_000, 20_000, 5, 9),
    ('m3x4', 86_400, 54_000, 6, 10),
    ('m6x4', 153_600, 96_000, 7, 11),
    ('m12x4', 268_800, 168_000, 8, 12),
)


def _tariffs() -> sa.Table:
    return sa.table(
        'tariffs',
        sa.column('code', sa.String),
        sa.column('title_ru', sa.String),
        sa.column('duration_days', sa.Integer),
        sa.column('price_kopeks', sa.Integer),
        sa.column('max_devices', sa.Integer),
        sa.column('sort_order', sa.Integer),
    )


def upgrade() -> None:
    tariffs = _tariffs()
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
    for code, _old_price, new_price, _old_order, new_order in REPRICED:
        bind.execute(
            sa.update(tariffs)
            .where(tariffs.c.code == code)
            .values(price_kopeks=new_price, sort_order=new_order)
        )


def downgrade() -> None:
    tariffs = _tariffs()
    bind = op.get_bind()
    for code, old_price, _new_price, old_order, _new_order in REPRICED:
        bind.execute(
            sa.update(tariffs)
            .where(tariffs.c.code == code)
            .values(price_kopeks=old_price, sort_order=old_order)
        )

    # Deleting a tariff someone has bought raises on the payments FK.
    # That is the correct outcome: rolling back on a database with
    # sales must fail loudly rather than drop money records.
    codes = ', '.join(f"'{code}'" for code, *_ in TARIFFS)
    op.execute(f'DELETE FROM tariffs WHERE code IN ({codes})')
    # Subscriptions sold on a three-, six- or eight-device tariff keep
    # their max_devices: the column survives this downgrade, and the
    # reconciler will go on pushing that number to the panel even
    # though nothing on sale can produce it any more.
