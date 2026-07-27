"""Router registration.

Routers are built per dispatcher rather than kept as module-level
singletons: aiogram refuses to attach one router to a second dispatcher,
which would make every test that builds its own dispatcher fail.
"""

from aiogram import Router

from app.bot.routers import start


def build_routers() -> tuple[Router, ...]:
    return (start.build_router(),)


__all__ = ['build_routers']
