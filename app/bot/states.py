"""FSM states — admin flows only; the user side is stateless."""

from aiogram.fsm.state import State, StatesGroup


class AdminFindUser(StatesGroup):
    waiting_for_query = State()


class AdminBroadcast(StatesGroup):
    waiting_for_message = State()


class AdminTariffPrice(StatesGroup):
    waiting_for_price = State()


class Support(StatesGroup):
    writing = State()
