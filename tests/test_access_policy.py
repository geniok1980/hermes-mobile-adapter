"""Доступ к агенту по токену приложения: без pairing и без allowlist.

Проверяем два уровня: собственную политику адаптера и решение настоящего ядра
Hermes (`GatewayAuthorizationMixin._own_policy_authorizes`) — оно и решает,
пускать сообщение к агенту или ответить кодом подтверждения.
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gateway.config import Platform, PlatformConfig  # noqa: E402

from hermes_mobile import adapter as mobile_adapter  # noqa: E402

from test_adapter_ws import _free_port, _register_platform  # noqa: E402

USER = "hermes-go-user-abcd1234"


def _adapter(extra: dict):
    return mobile_adapter.HermesMobileAdapter(PlatformConfig(enabled=True, extra=dict(extra)))


class TestOwnPolicy(unittest.TestCase):
    def setUp(self):
        _register_platform()
        self.port = _free_port()
        self.base = {"host": "127.0.0.1", "port": self.port, "path": "/ws", "token": "t" * 64}

    def test_adapter_declares_own_access_policy(self):
        adapter = _adapter(self.base)
        self.assertTrue(adapter.enforces_own_access_policy)
        self.assertEqual(adapter._dm_policy, "allowlist")
        self.assertEqual(adapter._group_policy, "disabled")

    def test_token_is_enough_without_allowlist(self):
        adapter = _adapter(self.base)
        self.assertTrue(adapter._is_dm_allowed(USER))
        self.assertTrue(adapter._is_dm_allowed("any-other-device-user"))

    def test_empty_principal_is_never_allowed(self):
        adapter = _adapter(self.base)
        self.assertFalse(adapter._is_dm_allowed(""))
        self.assertFalse(adapter._is_dm_allowed("   "))

    def test_allowlist_still_restricts(self):
        adapter = _adapter({**self.base, "allowed_users": f"other-id,{USER}"})
        self.assertTrue(adapter._is_dm_allowed(USER))
        self.assertFalse(adapter._is_dm_allowed("stranger"))

    def test_allowlist_accepts_platform_prefix(self):
        adapter = _adapter({**self.base, "allowed_users": f"mobile:{USER}"})
        self.assertTrue(adapter._is_dm_allowed(USER))


class TestHermesDecision(unittest.TestCase):
    """Настоящее решение ядра: собственный вердикт адаптера принимается."""

    def setUp(self):
        _register_platform()
        self.port = _free_port()
        self.base = {"host": "127.0.0.1", "port": self.port, "path": "/ws", "token": "t" * 64}

    def _gateway(self, adapter):
        from gateway.authz_mixin import GatewayAuthorizationMixin

        class Probe(GatewayAuthorizationMixin):
            def __init__(self, live_adapter):
                self.adapters = {Platform("mobile"): live_adapter}
                self.config = SimpleNamespace(platforms={})

            def _adapters_for_profile(self, profile):
                return self.adapters

        return Probe(adapter)

    def _source(self, user_id=USER):
        return SimpleNamespace(
            platform=Platform("mobile"),
            chat_type="dm",
            chat_id="mobile:dev-1",
            user_id=user_id,
        )

    def test_core_authorizes_token_holder_without_allowlist(self):
        adapter = _adapter(self.base)
        gateway = self._gateway(adapter)
        self.assertIs(gateway._own_policy_authorizes(self._source(), USER, False, None), True)

    def test_core_rejects_when_allowlist_excludes_user(self):
        adapter = _adapter({**self.base, "allowed_users": "someone-else"})
        gateway = self._gateway(adapter)
        self.assertIs(gateway._own_policy_authorizes(self._source(), USER, False, None), False)


if __name__ == "__main__":
    unittest.main()
