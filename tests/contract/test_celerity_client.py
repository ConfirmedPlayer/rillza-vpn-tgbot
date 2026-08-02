"""Contract tests for the CELERITY client, against mocked HTTP.

These lock in the panel behaviours that fail silently in production:
the create request must carry enabled+groups, renewals must send an
absolute date, and revoking must never delete.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from aioresponses import aioresponses
from loguru import logger

from app.integrations.celerity import (
    CelerityClient,
    PanelAuthError,
    PanelForbiddenError,
    PanelNotFoundError,
    PanelRateLimitedError,
    PanelUnavailableError,
)

BASE = 'https://panel.example.com'
GROUP_ID = '65f0aa0000000000000000aa'
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def user_payload(**overrides) -> dict:
    payload = {
        '_id': 'objectid',
        'userId': '42',
        'username': 'ivan',
        'enabled': True,
        'expireAt': '2026-07-31T12:00:00.000Z',
        'trafficLimit': 0,
        'maxDevices': 0,
        'subscriptionToken': 'abc123def456',
        'traffic': {'tx': 1024, 'rx': 2048},
        'password': 'secret-hysteria-password',
        'xrayUuid': '00000000-0000-0000-0000-000000000000',
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def client(settings):
    return CelerityClient(settings, attempts=3, backoff=0)


@pytest.fixture
def mocked():
    with aioresponses() as mock:
        yield mock


def last_body(mocked: aioresponses) -> dict:
    """The JSON body of the most recent request."""
    request = list(mocked.requests.values())[-1][-1]
    return request.kwargs['json']


class TestAuthAndErrors:
    async def test_api_key_header_is_sent(self, client, mocked) -> None:
        mocked.get(f'{BASE}/api/groups', payload=[])

        await client.list_groups()

        request = list(mocked.requests.values())[-1][-1]
        assert request.kwargs['headers']['X-API-Key'] == 'ck_test'
        await client.close()

    async def test_public_endpoints_send_no_key(self, client, mocked) -> None:
        """/api/info spends no rate-limit budget, so send no credentials."""
        mocked.get(f'{BASE}/api/info/tok', payload={'enabled': True})

        await client.get_subscription_info('tok')

        request = list(mocked.requests.values())[-1][-1]
        assert 'X-API-Key' not in request.kwargs['headers']
        await client.close()

    @pytest.mark.parametrize(
        'status, expected', [(401, PanelAuthError), (403, PanelForbiddenError)]
    )
    async def test_client_errors_are_typed(
        self, client, mocked, status, expected
    ) -> None:
        mocked.get(
            f'{BASE}/api/groups',
            status=status,
            payload={'error': 'Insufficient permissions'},
        )

        with pytest.raises(expected) as error:
            await client.list_groups()

        assert 'Insufficient permissions' in str(error.value)
        await client.close()

    async def test_auth_errors_are_not_retried(self, client, mocked) -> None:
        """A bad key is an answer, not a glitch — retrying just burns quota."""
        mocked.get(f'{BASE}/api/groups', status=401, payload={'error': 'nope'})

        with pytest.raises(PanelAuthError):
            await client.list_groups()

        assert len(list(mocked.requests.values())[-1]) == 1
        await client.close()


class TestRetries:
    async def test_server_errors_are_retried_then_succeed(
        self, client, mocked
    ) -> None:
        mocked.get(f'{BASE}/api/groups', status=502, payload={'error': 'bad'})
        mocked.get(f'{BASE}/api/groups', payload=[])

        assert await client.list_groups() == []
        assert len(list(mocked.requests.values())[-1]) == 2
        await client.close()

    async def test_retries_are_bounded(self, client, mocked) -> None:
        for _ in range(3):
            mocked.get(f'{BASE}/api/groups', status=503, payload={})

        with pytest.raises(PanelUnavailableError):
            await client.list_groups()

        assert len(list(mocked.requests.values())[-1]) == 3
        await client.close()

    async def test_rate_limit_is_retried_and_reports_retry_after(
        self, client, mocked
    ) -> None:
        for _ in range(3):
            mocked.get(
                f'{BASE}/api/groups',
                status=429,
                payload={'error': 'Rate limit exceeded'},
                headers={'Retry-After': '0'},
            )

        with pytest.raises(PanelRateLimitedError) as error:
            await client.list_groups()

        assert error.value.retry_after == 0
        await client.close()

    async def test_connection_failure_is_wrapped(self, client, mocked) -> None:
        import aiohttp

        for _ in range(3):
            mocked.get(
                f'{BASE}/api/groups', exception=aiohttp.ClientError('boom')
            )

        with pytest.raises(PanelUnavailableError):
            await client.list_groups()
        await client.close()


class TestGroups:
    async def test_group_is_resolved_by_name_and_cached(
        self, client, mocked, settings
    ) -> None:
        mocked.get(
            f'{BASE}/api/groups',
            payload=[
                {'_id': 'other', 'name': 'Другая'},
                {'_id': GROUP_ID, 'name': settings.panel_group_name},
            ],
        )

        first = await client.resolve_group_id()
        second = await client.resolve_group_id()

        assert first == second == GROUP_ID
        # Cached: one HTTP call for two lookups.
        assert len(list(mocked.requests.values())[-1]) == 1
        await client.close()

    async def test_missing_group_is_an_error(
        self, client, mocked, settings
    ) -> None:
        mocked.get(
            f'{BASE}/api/groups', payload=[{'_id': 'x', 'name': 'Иная'}]
        )

        with pytest.raises(PanelNotFoundError) as error:
            await client.resolve_group_id()

        assert settings.panel_group_name in str(error.value)
        await client.close()


class TestCreateUser:
    async def test_create_sends_enabled_and_groups_together(
        self, client, mocked, settings
    ) -> None:
        """The invariant that silently breaks VLESS when violated.

        The panel pushes a user to the Xray nodes only when the create
        request itself says enabled, and only to the groups listed in
        that same request.
        """
        mocked.get(
            f'{BASE}/api/groups',
            payload=[{'_id': GROUP_ID, 'name': settings.panel_group_name}],
        )
        mocked.post(f'{BASE}/api/users', status=201, payload=user_payload())

        user, created = await client.create_or_get_user('42', NOW, 'ivan')

        body = last_body(mocked)
        assert body['enabled'] is True
        assert body['groups'] == [GROUP_ID]
        assert body['userId'] == '42'
        assert body['trafficLimit'] == 0
        assert body['maxDevices'] == 0
        assert created is True
        assert user.subscription_token == 'abc123def456'
        await client.close()

    async def test_conflict_returns_the_existing_user(
        self, client, mocked
    ) -> None:
        mocked.post(
            f'{BASE}/api/users',
            status=409,
            payload={
                'error': 'Пользователь уже существует',
                'user': user_payload(),
            },
        )
        mocked.get(f'{BASE}/api/users/42', payload=user_payload())

        user, created = await client.create_or_get_user(
            '42', NOW, group_id=GROUP_ID
        )

        assert created is False
        assert user.user_id == '42'
        await client.close()

    async def test_never_expires_is_sent_as_null(self, client, mocked) -> None:
        mocked.post(
            f'{BASE}/api/users',
            status=201,
            payload=user_payload(expireAt=None),
        )

        user, _ = await client.create_or_get_user(
            '42', None, group_id=GROUP_ID
        )

        assert last_body(mocked)['expireAt'] is None
        assert user.never_expires is True
        await client.close()


class TestRenewAndRevoke:
    async def test_renewal_sends_an_absolute_date(
        self, client, mocked
    ) -> None:
        """Never "current + 30 days": a retry would extend twice."""
        target = NOW + timedelta(days=30)
        mocked.put(f'{BASE}/api/users/42', payload=user_payload())

        await client.set_expiry('42', target)

        body = last_body(mocked)
        assert body == {'expireAt': target.isoformat()}
        await client.close()

    async def test_renewal_is_idempotent_under_retry(
        self, client, mocked
    ) -> None:
        target = NOW + timedelta(days=30)
        mocked.put(f'{BASE}/api/users/42', status=500, payload={})
        mocked.put(f'{BASE}/api/users/42', payload=user_payload())

        await client.set_expiry('42', target)

        sent = [
            call.kwargs['json']['expireAt']
            for call in list(mocked.requests.values())[-1]
        ]
        assert sent == [target.isoformat(), target.isoformat()]
        await client.close()

    async def test_revoke_disables_and_keeps_the_account(
        self, client, mocked
    ) -> None:
        """Deleting would revive a leaked hysteria2:// link on recreate."""
        mocked.put(f'{BASE}/api/users/42', payload=user_payload(enabled=False))

        user = await client.revoke('42')

        assert last_body(mocked) == {'enabled': False}
        assert user.enabled is False
        await client.close()

    async def test_client_offers_no_delete(self) -> None:
        assert not hasattr(CelerityClient, 'delete_user')

    async def test_client_offers_no_enable(self) -> None:
        """POST /enable validates nothing; renewal goes through expiry."""
        assert not hasattr(CelerityClient, 'enable_user')


class TestReads:
    async def test_missing_user_is_none_not_an_error(
        self, client, mocked
    ) -> None:
        mocked.get(
            f'{BASE}/api/users/42',
            status=404,
            payload={'error': 'Пользователь не найден'},
        )

        assert await client.get_user('42') is None
        await client.close()

    async def test_user_parses_panel_document(self, client, mocked) -> None:
        mocked.get(f'{BASE}/api/users/42', payload=user_payload())

        user = await client.get_user('42')

        assert user is not None
        assert user.expire_at == datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        assert user.traffic.total_bytes == 3072
        assert user.enabled is True
        await client.close()

    async def test_paginated_listing_reports_total(
        self, client, mocked
    ) -> None:
        mocked.get(
            f'{BASE}/api/users?page=1&limit=2',
            payload={
                'users': [user_payload(), user_payload(userId='43')],
                'pagination': {'page': 1, 'limit': 2, 'total': 7, 'pages': 4},
            },
        )

        users, total = await client.iter_users(page=1, limit=2)

        assert [u.user_id for u in users] == ['42', '43']
        assert total == 7
        await client.close()

    async def test_device_limit_is_read_off_the_expanded_group(
        self, client, mocked
    ) -> None:
        """The only way to see the cap the panel will enforce.

        GET /api/groups answers with ids and names only, so the group's
        maxDevices reaches us just once: expanded inside an account that
        belongs to it.
        """
        mocked.get(
            f'{BASE}/api/users?page=1&limit=1',
            payload={
                'users': [
                    user_payload(
                        maxDevices=0,
                        groups=[
                            {
                                '_id': GROUP_ID,
                                'name': 'Celerity Primary Access',
                                'maxDevices': 2,
                            }
                        ],
                    )
                ],
                'pagination': {'total': 1},
            },
        )

        users, _ = await client.iter_users(page=1, limit=1)

        assert users[0].effective_device_limit == 2
        await client.close()

    async def test_unexpanded_groups_are_not_a_parse_error(
        self, client, mocked
    ) -> None:
        """Create and 409 answer with bare ObjectIds, not group objects.

        Both shapes are normal, so neither may blow up provisioning.
        """
        mocked.get(
            f'{BASE}/api/users/42',
            payload=user_payload(groups=[GROUP_ID], maxDevices=0),
        )

        user = await client.get_user('42')

        assert user is not None
        assert user.groups == []
        # Nothing said about groups, so nothing claimed about the cap.
        assert user.effective_device_limit == 0
        await client.close()

    async def test_an_override_hides_the_group_limit_but_not_its_value(
        self, client, mocked
    ) -> None:
        """A hand-made account can say -1 and never consult its group.

        Reading only the resolved number then reports "no limit" while
        the group is capped correctly — which is exactly how a working
        setup got misread once. The group's own value stays legible.
        """
        mocked.get(
            f'{BASE}/api/users/42',
            payload=user_payload(
                userId='Sasha',
                maxDevices=-1,
                groups=[
                    {
                        '_id': GROUP_ID,
                        'name': 'Celerity Primary Access',
                        'maxDevices': 2,
                    }
                ],
            ),
        )

        user = await client.get_user('42')

        assert user is not None
        assert user.effective_device_limit == -1
        assert [g.max_devices for g in user.groups] == [2]
        await client.close()

    async def test_an_account_override_beats_the_group(
        self, client, mocked
    ) -> None:
        mocked.get(
            f'{BASE}/api/users/42',
            payload=user_payload(
                maxDevices=5,
                groups=[{'_id': GROUP_ID, 'name': 'G', 'maxDevices': 2}],
            ),
        )

        user = await client.get_user('42')

        assert user is not None
        assert user.effective_device_limit == 5
        await client.close()

    async def test_subscription_info_reads_live_traffic(
        self, client, mocked
    ) -> None:
        mocked.get(
            f'{BASE}/api/info/tok',
            payload={
                'enabled': True,
                'expire': '2026-07-31T12:00:00.000Z',
                'servers': 3,
                'traffic': {'used': 5_000, 'limit': 0, 'tx': 2000, 'rx': 3000},
            },
        )

        info = await client.get_subscription_info('tok')

        assert info is not None
        assert info.servers == 3
        assert info.traffic.total_bytes == 5_000
        assert info.traffic.unlimited is True
        await client.close()

    async def test_unknown_token_is_none(self, client, mocked) -> None:
        mocked.get(
            f'{BASE}/api/info/nope', status=404, payload={'error': 'Not found'}
        )

        assert await client.get_subscription_info('nope') is None
        await client.close()

    async def test_health_is_public(self, client, mocked) -> None:
        mocked.get(
            f'{BASE}/health',
            payload={'status': 'ok', 'uptime': 1234.5, 'isSyncing': False},
        )

        health = await client.health()

        assert health.status == 'ok'
        assert health.is_syncing is False
        await client.close()


class TestSync:
    async def test_sync_posts_without_body(self, client, mocked) -> None:
        mocked.post(f'{BASE}/api/sync', payload={'message': 'Sync started'})

        await client.sync()

        request = list(mocked.requests.values())[-1][-1]
        assert request.kwargs['json'] is None
        await client.close()


class TestSecrets:
    async def test_api_key_is_not_in_repr(self, settings) -> None:
        client = CelerityClient(settings)
        assert 'ck_test' not in repr(client)
        await client.close()

    async def test_panel_credentials_never_reach_logs(
        self, client, mocked
    ) -> None:
        """Retry logging prints the URL and error, never the key.

        loguru does not feed pytest's caplog, so capture its own sink —
        otherwise this assertion would inspect an empty list and pass
        without proving anything.
        """
        captured: list[str] = []
        sink_id = logger.add(captured.append, level='DEBUG')
        try:
            for _ in range(3):
                mocked.get(f'{BASE}/api/groups', status=503, payload={})

            with pytest.raises(PanelUnavailableError):
                await client.list_groups()
        finally:
            logger.remove(sink_id)

        # The retries did log, so the assertion below is not vacuous.
        assert any('/api/groups' in message for message in captured)
        assert 'ck_test' not in json.dumps(captured)
        await client.close()


#: Captured verbatim from the live panel on 2026-08-02. Two nodes, both
#: up, two users connected — all of them through the Xray node.
LIVE_STATS = {
    'users': {'total': 7, 'enabled': 7},
    'nodes': {'total': 2, 'online': 2},
    'onlineUsers': 2,
    'nodesList': [
        {'name': 'Hysteria 2', 'online': 0},
        {'name': 'Xray TCP', 'online': 2},
    ],
    'lastSync': '2026-08-02T09:45:00.221Z',
}


class TestFleetStats:
    """``nodesList[].online`` counts users, it is not an up/down flag.

    Reading it as a boolean broke the admin screen outright once a node
    carried more than one user, and before that it quietly called every
    idle node offline — the one alarm that is supposed to mean a node
    has silently dropped out of every subscription.
    """

    async def test_the_panels_own_payload_parses(self, client, mocked) -> None:
        mocked.get(f'{BASE}/api/stats', payload=LIVE_STATS)

        stats = await client.stats()

        assert stats.nodes_total == 2
        assert stats.nodes_online == 2
        assert stats.online_users == 2
        await client.close()

    async def test_an_idle_node_is_not_reported_offline(
        self, client, mocked
    ) -> None:
        mocked.get(f'{BASE}/api/stats', payload=LIVE_STATS)

        stats = await client.stats()

        # Hysteria carries nobody right now; that is not an outage.
        assert stats.nodes_offline == 0
        await client.close()

    async def test_a_node_that_is_down_is_counted(
        self, client, mocked
    ) -> None:
        payload = LIVE_STATS | {'nodes': {'total': 2, 'online': 1}}
        mocked.get(f'{BASE}/api/stats', payload=payload)

        stats = await client.stats()

        assert stats.nodes_offline == 1
        await client.close()

    async def test_per_node_user_counts_survive(self, client, mocked) -> None:
        mocked.get(f'{BASE}/api/stats', payload=LIVE_STATS)

        stats = await client.stats()

        counts = {node.name: node.online_users for node in stats.nodes_list}
        assert counts == {'Hysteria 2': 0, 'Xray TCP': 2}
        await client.close()
