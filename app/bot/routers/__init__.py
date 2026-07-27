"""Routers are registered explicitly, in order.

The legacy bot auto-imported every module in this package; an explicit
list keeps registration order obvious and makes a typo a hard error.
"""

from aiogram import Router

from app.bot.routers import start

ROUTERS: tuple[Router, ...] = (start.router,)

__all__ = ['ROUTERS']
