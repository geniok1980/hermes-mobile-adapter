"""Кадры каталога команд и списка моделей (issue #23).

Проверяем три вещи: форму кадров, сбор данных из реестра/инвентаря ядра (с заглушками —
настоящие модули ядра в тестах недоступны) и то, что запросы приложения доходят до
отправки кадра. Каталог не критичен для разговора: недоступный источник обязан давать
пустой ответ, а не падать.
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapter.hermes_mobile import adapter as adapter_module  # noqa: E402
from adapter.hermes_mobile import protocol as proto  # noqa: E402


class FakeCommand:
    def __init__(self, name, args="", description="", category="Session", cli_only=False, aliases=()):
        self.name = name
        self.args_hint = args
        self.description = description
        self.category = category
        self.cli_only = cli_only
        self.aliases = aliases

    def describe(self):
        return f"{self.description} (ru)"


def with_commands_module(registry):
    module = types.ModuleType("hermes_cli.commands")
    module.COMMAND_REGISTRY = registry
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.commands = module
    return patch.dict(sys.modules, {"hermes_cli": hermes_cli, "hermes_cli.commands": module})


def with_inventory_module(payload):
    module = types.ModuleType("hermes_cli.inventory")
    module.load_picker_context = lambda: {"stub": True}
    module.build_model_options_payload = lambda ctx, **kwargs: payload
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.inventory = module
    return patch.dict(sys.modules, {"hermes_cli": hermes_cli, "hermes_cli.inventory": module})


class FramesTest(unittest.TestCase):
    def test_commands_frame_shape(self):
        frame = proto.commands_frame(
            [{"name": "new", "args": "[name]", "description": "Начать сессию", "category": "Session"}],
        )
        self.assertEqual(frame["t"], proto.S_COMMANDS)
        self.assertEqual(frame["v"], proto.PROTOCOL_VERSION)
        self.assertEqual(frame["commands"][0]["name"], "new")
        # Кадр обязан быть сериализуемым в JSON — им уходит приложению.
        self.assertIn("commands", proto.loads(proto.dumps(frame)))

    def test_models_frame_shape(self):
        frame = proto.models_frame("opencode-go", "deepseek-v4.1-flash", ["a", "b"])
        self.assertEqual(frame["t"], proto.S_MODELS)
        self.assertEqual(frame["provider"], "opencode-go")
        self.assertEqual(frame["current"], "deepseek-v4.1-flash")
        self.assertEqual(frame["models"], ["a", "b"])

    def test_empty_values_are_tolerated(self):
        frame = proto.models_frame("", "", [])
        self.assertEqual(frame["provider"], "")
        self.assertEqual(frame["current"], "")
        self.assertEqual(frame["models"], [])


class CollectCommandsTest(unittest.TestCase):
    def test_takes_names_descriptions_and_skips_cli_only(self):
        registry = [
            FakeCommand("new", "[name]", "Start a session"),
            FakeCommand("status", "", "Show status", category="Info", aliases=("st",)),
            FakeCommand("wake", "", "CLI only", cli_only=True),
        ]
        with with_commands_module(registry):
            catalog = adapter_module.collect_command_catalog()

        names = [item["name"] for item in catalog]
        self.assertEqual(names, ["new", "status"])
        self.assertEqual(catalog[0]["args"], "[name]")
        self.assertEqual(catalog[0]["description"], "Start a session (ru)")
        self.assertEqual(catalog[1]["category"], "Info")
        self.assertEqual(catalog[1]["aliases"], ["st"])

    def test_missing_registry_gives_empty_catalog(self):
        with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.commands": None}):
            self.assertEqual(adapter_module.collect_command_catalog(), [])

    def test_broken_registry_does_not_raise(self):
        module = types.ModuleType("hermes_cli.commands")
        module.COMMAND_REGISTRY = [object()]  # без нужных атрибутов
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.commands = module
        with patch.dict(sys.modules, {"hermes_cli": hermes_cli, "hermes_cli.commands": module}):
            catalog = adapter_module.collect_command_catalog()
        self.assertEqual(catalog, [])


class CollectModelsTest(unittest.TestCase):
    PAYLOAD = {
        "provider": "opencode-go",
        "model": "deepseek-v4.1-flash",
        "providers": [
            {"slug": "opencode-go", "models": [{"name": "deepseek-v4-flash"}, "kimi-k2.6", {"id": "glm-5.1"}]},
            {"slug": "other", "models": [{"name": "не-наш-провайдер"}]},
        ],
    }

    def test_collects_current_provider_models(self):
        with with_inventory_module(self.PAYLOAD):
            result = adapter_module.collect_model_options()

        self.assertIsNotNone(result)
        provider, current, models = result
        self.assertEqual(provider, "opencode-go")
        self.assertEqual(current, "deepseek-v4.1-flash")
        # Названия берём и из строк, и из словарей; чужой провайдер не подмешиваем.
        self.assertEqual(models, ["deepseek-v4-flash", "kimi-k2.6", "glm-5.1"])

    def test_empty_listing_gives_none(self):
        with with_inventory_module({"provider": "opencode-go", "model": "x", "providers": []}):
            self.assertIsNone(adapter_module.collect_model_options())

    def test_missing_inventory_gives_none(self):
        with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.inventory": None}):
            self.assertIsNone(adapter_module.collect_model_options())


class SendTest(unittest.TestCase):
    """Запросы приложения должны доходить до отправки кадра (без живого WS)."""

    class StubAdapter:
        def __init__(self):
            self.sent = []

        async def _push(self, device, frame):
            self.sent.append(frame)
            return True

    def test_commands_request_sends_catalog(self):
        stub = self.StubAdapter()
        with with_commands_module([FakeCommand("new", "", "Start")]):
            asyncio.run(adapter_module.HermesMobileAdapter._send_command_catalog(stub, "device"))
        self.assertEqual(stub.sent[0]["t"], proto.S_COMMANDS)
        self.assertEqual(stub.sent[0]["commands"][0]["name"], "new")

    def test_models_request_sends_list(self):
        stub = self.StubAdapter()
        with with_inventory_module(CollectModelsTest.PAYLOAD):
            asyncio.run(adapter_module.HermesMobileAdapter._send_model_options(stub, "device"))
        self.assertEqual(stub.sent[0]["t"], proto.S_MODELS)
        self.assertEqual(stub.sent[0]["models"][0], "deepseek-v4-flash")

    def test_unavailable_sources_send_nothing(self):
        stub = self.StubAdapter()
        with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.commands": None}):
            asyncio.run(adapter_module.HermesMobileAdapter._send_command_catalog(stub, "device"))
        with patch.dict(sys.modules, {"hermes_cli": None, "hermes_cli.inventory": None}):
            asyncio.run(adapter_module.HermesMobileAdapter._send_model_options(stub, "device"))
        self.assertEqual(stub.sent, [])


class DispatchTest(unittest.TestCase):
    """Кадры-запросы приложения должны попадать в свои обработчики."""

    class StubAdapter:
        def __init__(self):
            self.calls = []

        async def _send_command_catalog(self, device):
            self.calls.append("commands")

        async def _send_model_options(self, device):
            self.calls.append("models")

    def test_requests_are_routed(self):
        stub = self.StubAdapter()

        class Device:
            last_seen = 0.0

        async def run():
            await adapter_module.HermesMobileAdapter._dispatch(stub, Device(), None, '{"t": "commands"}')
            await adapter_module.HermesMobileAdapter._dispatch(stub, Device(), None, '{"t": "models"}')

        asyncio.run(run())
        self.assertEqual(stub.calls, ["commands", "models"])

    def test_unknown_frame_is_ignored(self):
        stub = self.StubAdapter()

        class Device:
            last_seen = 0.0

        asyncio.run(adapter_module.HermesMobileAdapter._dispatch(stub, Device(), None, '{"t": "нет-такого"}'))
        self.assertEqual(stub.calls, [])


if __name__ == "__main__":
    unittest.main()
