"""Router registration.

Routers are built per dispatcher rather than kept as module-level
singletons: aiogram refuses to attach one router to a second dispatcher,
which would make every test that builds its own dispatcher fail.
"""

from aiogram import Router

from app.bot.routers import admin, buy, errors, menu, support
from app.core.settings import Settings


def build_routers(settings: Settings) -> tuple[Router, ...]:
    # Admin first: its filters claim admin-only callbacks before the
    # user routers see them.
    return (
        admin.build_router(settings),
        menu.build_router(),
        buy.build_router(),
        support.build_router(),
        errors.build_router(),
    )


__all__ = ['build_routers']
