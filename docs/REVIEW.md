# Ревью перед запуском: что осталось

> **ЭТОТ ФАЙЛ — ИСТОРИЧЕСКИЙ СНИМОК. ВСЁ, ЧТО В НЁМ ПЕРЕЧИСЛЕНО,
> ЗАКРЫТО.** Он написан коммитом `d04f8b3` и с тех пор не менялся, а
> находки чинились после него — `4d978ec`, `724848a`, `be7e2ea`,
> `5435bf6`, `550d566`. Не берите отсюда задачи: половину рабочего дня
> можно потратить на починку уже починенного.
>
> Ценность файла в другом — в рассуждениях скептиков. Там записано,
> **почему** решение принято именно такое, и этого больше нигде нет.
> Читать как справку по мотивам, а не как список дел.
>
> Что действительно открыто — в `handoff.json`.

Состязательное ревью прогонялось агентами по шести осям (деньги, панель,
база и конкурентность, безопасность, поведение бота, эксплуатация).
31 заявка, 8 проверено скептиком, 8 подтверждено, 0 опровергнуто.

Три высокие исправлены — коммиты `a0ed6f4` (двойное начисление дней) и
`b2e4116` (подмена черновика рассылки). Ниже то, что осталось.

Формат: место в коде, чем оборачивается, и вердикт проверяющего там, где
он был. Всё остальное — из прогона, который в живой сессии больше не
поднять, поэтому записано целиком.

### Подтверждённые, ещё не исправленные

#### `app/services/trial_service.py:65` — Trial grant re-provisions a REVOKED subscription, restoring panel access the admin took away

**Серьёзность:** medium

`TrialService.grant()` branches only on `subscription_token is None` and never looks at `subscription.status`, so any existing subscription without a token — including a REVOKED one — is pushed through `_finish()` -> `SubscriptionService.provision()`, which creates/enables the panel account and sets the row back to ACTIVE.

**Сценарий отказа.** User 42 gets a trial while the panel is down: `_finish` catches PanelError and leaves the row PENDING with `subscription_token = None` (trial_service.py:88). An admin then revokes 42 (admin.py:188): `SubscriptionService.revoke()` commits status=REVOKED, and `panel.revoke()` raises PanelNotFoundError because no panel account was ever created — the admin sees PANEL_UNAVAILABLE, the DB says REVOKED. The user scrolls up to the still-tappable "подтвердить пробный период" message and presses it. `handle_trial_confirm` (menu.py:92) calls `grant()` with no `is_available()` check; `existing` is not None and its token is None, so line 65-66 calls `_finish` -> `provision()`, which POSTs `/api/users` with `enabled: true` and the future trial expiry, stores the fresh token, and sets `subscription.status = ACTIVE` (subscription_service.py:99). The revoked user now has a working link and an ACTIVE row; reconcile will not undo it because it reads status, which is now ACTIVE.

**Вердикт скептика.** Confirmed by reading and by execution. app/services/trial_service.py:62-67 branches only on `existing.subscription_token is None` and never inspects `subscription.status`; app/bot/routers/menu.py:86-94 calls `grant()` with no `is_available()` check and `handle_trial_offer` (menu.py:81) has no guard either, so a stale `trial:offer`/`trial:confirm` button re-enters the flow after main_menu() stops drawing it. app/services/subscription_service.py:154-162 commits status=REVOKED before calling the panel, so an admin revoking a never-provisioned row (admin.py:187-191, PanelNotFoundError from PUT /api/users/{id}) leaves REVOKED + subscription_token=None; the next tap runs _finish -> provision(), which POSTs the account with enabled:true (client.py:255-274) and sets status=ACTIVE (subscription_service.py:99). Reconcile has exactly this guard (reconcile_service.py:114-120, "creating it would restore access") and will not undo it once the row reads ACTIVE. I wrote a throwaway integration test against the real Postgres fixture: panel offline -> PENDING_PROVISIONING; revoke() commits REVOKED then raises; second grant() -> GRANTED, panel user 42 created enabled with a future expiry, row back to active with a fresh token. No existing test covers it: tests/integration/test_trial_flow.py:177 never revokes, tests/integration/test_admin_flow.py:235 revokes only a provisioned subscription. mark_expired (repositories/subscriptions.py:66-87) is ACTIVE-only so PENDING cannot drift to EXPIRED, and a normal revoke keeps the token and correctly hits HAS_SUBSCRIPTION — so the trigger is narrow: a trial that died before the panel answered, an admin revoke of that pending row, and a still-live trial button. Real wrong-access defect, but I rate it medium rather than high because of that three-step chain.


#### `app/services/reconcile_service.py:107` — Reconciler disables the panel account of a user who renewed while it was running

**Серьёзность:** medium

`run()` materialises every subscription up front (line 62) and keeps the ORM objects alive for the whole loop, and `_reconcile_one` classifies each one from `subscription.status` as read at that moment. Nothing re-reads the row before acting, so a status that changed during the run is not seen: an EXPIRED-at-load subscription is still treated as "must not be active" and `panel.revoke()` is called on it (line 124). The same stale read also feeds `subscriptions.extend(subscription, subscription.expires_at)` at lines 131 and 140, which pushes a pre-renewal expiry back onto the panel.

**Сценарий отказа.** A user's subscription lapsed (status EXPIRED, panel account still enabled — nothing disables it on expiry). The reconciler starts (every 4 h), snapshots the panel and the subscription table. While it is working through the list, the user renews: the payment poller marks them ACTIVE, pushes the new expiry to the panel, and the user gets their link. The reconciler reaches their row, still sees EXPIRED, and calls `panel.revoke()`. Reproduced on PostgreSQL: after the renewal the database says `active` until +30 d while the panel account is `enabled=False`, i.e. a customer who has just paid has no VPN at all until the next reconcile run up to 4 hours later.

**Вердикт скептика.** Survives. The code says what the claim says and no guard prevents it. app/services/reconcile_service.py:62 materialises every Subscription up front, and app/db/engine.py:24 builds the sessionmaker with expire_on_commit=False, so the in-loop commits (reconcile_service.py:149, plus those inside provision/extend/push_expiry) never refresh the identity map — the status read at line 107 stays frozen for the whole run. Line 122-124 then calls CelerityClient.revoke (client.py:291, a hard PUT enabled:false) with no re-read of the row and no row lock; SubscriptionsRepository.lock_by_user (subscriptions.py:24) exists and is used by PaymentService._apply_days (payment_service.py:237) but not by the reconciler, so FOR UPDATE gives it no protection. Concurrency is reachable in a single process: app/main.py:100-115 runs AsyncIOScheduler and start_polling on one event loop and JobRunner.run (scheduler/jobs.py:80) opens a fresh session per job, so the 30 s payment poller (or a user tapping "проверить оплату") interleaves at every await; the renewal sets status=ACTIVE and the new expiry (payment_service.py:256-264) and pushes it (payment_service.py:275). The authors knew about the stale read — subscription_service.py:118-119 names "a reconciler working from an old read" — but the `until > expires_at` guard only protects the DB row; the EXPIRED branch bypasses extend entirely, and push_expiry (subscription_service.py:138-140) sends the stale date to the panel. No test in tests/integration/test_reconcile.py sets up any interleaving (test_revoked_subscription_is_re_disabled:119 and test_user_disabled_behind_our_back_is_restored:105 are steady-state), and it is not on the intentional list. I verified one part of the claim is overstated: SQLAlchemy only UPDATEs mutated attributes, so lines 131/140 do not write a pre-renewal expiry back into the database — only onto the panel. Downgrading high to medium: the failure needs the row EXPIRED at load AND the panel snapshot still enabled (only true for users who lapsed since the previous run — line 123 skips the rest) AND the renewal to land inside the loop's execution window, roughly a second every four hours; and it self-repairs at the next run via reconcile_service.py:137-144. Real access loss for a paying customer, bounded to one user for up to 4 h, not a deterministic money/access defect.


#### `app/bot/routers/buy.py:68` — Deactivated and archived tariffs stay purchasable at their old price via crafted callback data

**Серьёзность:** medium

The purchase path resolves the tariff with `uow.tariffs.get(id)`, which applies no `is_active` / `is_archived` filter, so any tariff row the operator has switched off can still be selected and invoiced with its stored price and duration.

**Сценарий отказа.** Callback data is client-supplied — an MTProto client can call getBotCallbackAnswer with arbitrary `data`, it does not have to match a button the bot sent. The listing query `TariffsRepository.list_active` (app/repositories/tariffs.py:24-31) filters on `is_active AND NOT is_archived`, but both purchase handlers use `TariffsRepository.get` (app/repositories/tariffs.py:15-16), which fetches by primary key only: `handle_tariff` at buy.py:37 and `handle_provider` at buy.py:68. Concretely: the operator runs a launch promo `promo12` = 365 days for 100 RUB, then sets `is_active=false` on it (the admin tariff screen renders it as paused, app/bot/texts/admin.py:95). Tariff id 5 stays in the table forever because payments FK it. An attacker sends callback `provider:5:yoomoney`; buy.py:68 returns the retired row, `PaymentService.create_invoice` (app/services/payment_service.py:95-105) writes `amount_kopeks=10000`, a 100 RUB YooMoney invoice is issued, and on confirmation `_mark_paid`/`_apply_days` (payment_service.py:199, 248) add `tariff.duration_days = 365`. The user gets a year of VPN for 100 RUB, indefinitely, and the same works for `is_archived=true` rows the operator considers dead. Turning a tariff off in the admin screen does not actually stop sales of it.

**Вердикт скептика.** The claim holds as written. app/repositories/tariffs.py:15-16 `get()` is a bare `session.get(Tariff, id)` (primary key only), while only `list_active()` (tariffs.py:24-31) filters `is_active AND NOT is_archived`. Both purchase handlers use the unfiltered getter — app/bot/routers/buy.py:37 (`handle_tariff`) and buy.py:68 (`handle_provider`) — and the sole rejection is `tariff is None`. From there app/services/payment_service.py:90,101 invoices `tariff.price_kopeks` and payment_service.py:199,248 grant `tariff.duration_days`; `handle_check` re-derives the tariff from `payment.tariff_id`, so nothing revalidates it at finalize either.


#### `app/bot/routers/admin.py:166` — Admin grant retried after a panel outage adds the days twice

**Серьёзность:** medium

`handle_grant` commits the subscription before the panel call in both branches — `create_pending` commits at subscription_service.py:74 and `extend` commits the new `expires_at` at subscription_service.py:126 — but a `PanelError` from `provision`/`push_expiry` is reported to the admin as «Панель недоступна» with no indication that the days already landed, and the user card is not refreshed (early `return` at line 168). A retry re-adds the same days on top of the already-committed expiry.

**Сценарий отказа.** User has an active subscription until 31 Jan. Admin taps «➕ 30 дней». `extend` commits expires_at = 2 Mar, then `push_expiry` raises `PanelError` because the panel is down. The admin sees «Панель недоступна» and the stale card still showing 31 Jan, so they tap «➕ 30 дней» again once the panel is back: `base = max(now, 2 Mar)` → expires_at = 1 Apr. One 30-day grant became 60 free days, and the reconciler then pushes the doubled date to the panel because the database is the source of truth. Same double-count via the create branch: `create_pending` commits now+30, `provision` fails, the retry falls into the `else` branch and extends to now+60.

**Вердикт скептика.** Confirmed by reading the code and by an end-to-end repro against real Postgres with the project's own dispatcher/FakePanel harness (temp test since deleted).


### Не проверялись — отсечка была на восьми

Это заявки ревьюеров, через скептика они не проходили.
Часть может оказаться нереальной; проверять перед починкой.

| Серьёзность | Место | Заявка |
|---|---|---|
| high | `app/scheduler/jobs.py:202` | Late-payment sweep window equals its interval, so each expired invoice is re-checked exactly once — money arriving after that single check is lost |
| medium | `app/services/payment_service.py:208` | Lock-order inversion between the payment row and the subscription row deadlocks the finalizer |
| medium | `app/services/payment_service.py:355` | A late payment reopened by the sweep and then not finalised is never looked at again |
| medium | `app/services/reconcile_service.py:124` | Reconcile acts on a DB snapshot it never re-reads, so it can disable a user who paid mid-run |
| medium | `app/services/reconcile_service.py:146` | Reconcile never repairs a PENDING row whose panel account is already healthy |
| medium | `app/services/subscription_service.py:144` | push_expiry's 404 fallback accepts an existing panel account without pushing the expiry, then reports success |
| medium | `app/services/subscription_service.py:74` | create_pending() commits inside _apply_days, so a first purchase's days and its idempotency latch are not atomic |
| medium | `app/services/rate_limit.py:23` | Non-atomic INCR/EXPIRE can lock a user out of support permanently |
| medium | `app/services/support_service.py:157` | Admin's support reply to a user who blocked the bot surfaces as a generic bot error |
| medium | `app/bot/middlewares/user_upsert.py:27` | is_bot_blocked is set but never cleared, permanently excluding users who unblock the bot from broadcasts |
| medium | `alembic/env.py:24` | Any '%' in DATABASE_URL makes `alembic upgrade head` raise, so the container crash-loops and the bot never starts |
| medium | `app/main.py:123` | Scheduler is shut down last, so a running job keeps executing against an already-closed bot session — a resuming broadcast marks its whole remaining audience 'failed' and then DONE |
| medium | `app/services/broadcast_service.py:67` | Broadcast resumer can start a second concurrent run on a broadcast that is still healthily sending, duplicating messages |
| medium | `app/services/payment_service.py:299` | poll_pending holds FOR UPDATE row locks on every live invoice for the whole run, so the manual 'проверить оплату' button answers BUSY for minutes |
| low | `app/integrations/payments/cryptobot.py:118` | CryptoBot invoice id is stored as the literal string 'None' when the response omits invoice_id |
| low | `app/bot/routers/admin.py:162` | Admin grant bypasses lock_by_user and can overwrite a concurrently applied payment, moving the expiry backwards |
| low | `app/services/payment_service.py:249` | Two concurrent first payments for the same user hit the subscriptions unique constraint |
| low | `app/services/support_service.py:116` | A support request and its reply routing are persisted only when delivery fully succeeds |
| low | `app/bot/routers/admin.py:188` | Revoke commits REVOKED before the panel call, then reports "panel unavailable" without refreshing the card |
| low | `app/bot/routers/buy.py:37` | Tariff lookup on tap ignores is_active/is_archived, so a stale keyboard can still buy a withdrawn plan |
| low | `app/bot/routers/menu.py:150` | A plain text message from a user not in the support flow gets no reply at all |
| low | `app/services/rate_limit.py:24` | Rate-limiter INCR without a guaranteed EXPIRE can lock a user out of support permanently |
| low | `app/services/notification_service.py:81` | Flood control during expiry reminders permanently burns the reminder it was sending and aborts the rest of the run |

## Как это чинить

Порядок по цене ошибки, а не по серьёзности из таблицы:

1. `trial_service.py:65` — отозванная подписка воскресает. Тут админ
   теряет единственный рычаг против злоупотребления.
2. `payment_service.py`/`jobs.py:202` — поздние деньги. Клиент заплатил
   и не получил доступ; узнаете об этом только из его жалобы.
3. `reconcile_service.py` — сверка выключает того, кто только что
   продлился. Доступ отваливается у платящего клиента.
4. `buy.py:68` — снятый тариф покупается по старой цене через
   подделанные callback-данные.
5. `admin.py:166` — ручная выдача после сбоя панели удваивает дни.

Непроверенные из таблицы сначала подтвердить: половина обычно
оказывается уже перехваченной защитой в другом месте.

## Чем проверять исправление

Каждое исправление должно сопровождаться тестом, который **падает на
коде до правки**. Это не формальность: в этой же работе первая пара
тестов на двойное начисление проходила и на сломанном коде, то есть не
проверяла ничего. Способ убедиться — откатить правку, увидеть падение,
вернуть.

Интеграционные тесты требуют настоящий PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost:5432/rillza_test \
  uv run pytest
```
