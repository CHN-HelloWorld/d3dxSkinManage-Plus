"""通过真实插件验证依赖顺序、公开 API 及故障传播。"""

import json

from tests.plugins.test_activation import lazy_plugin, prepare
from tests.plugins.test_runtime import env as env

PROVIDER = """from src.plugin_sdk import PluginBase
class Plugin(PluginBase):
    def activate(self, ctx):
        self.ctx = ctx
        ctx.commands.register(ctx.plugin_id + ".run", lambda: 41)
        return {"answer": 41}
"""
CONSUMER = """from src.plugin_sdk import PluginBase
class Plugin(PluginBase):
    def activate(self, ctx):
        self.ctx = ctx
        self.api = ctx.extensions.get_exports("test.provider")
        ctx.commands.register(ctx.plugin_id + ".run", lambda: self.api["answer"] + 1)
"""


def dependency_pair(directory):
    """创建共享 API 提供者和声明依赖的消费者。"""
    provider = lazy_plugin(directory, "test.provider")
    (provider / "entry.py").write_text(PROVIDER, encoding="utf-8")
    consumer = lazy_plugin(directory, "test.consumer", dependencies={"test.provider": "^1.0.0"})
    (consumer / "entry.py").write_text(CONSUMER, encoding="utf-8")
    return provider, consumer


def test_dependency_activates_before_consumer_and_exports_are_available(env):
    manager, directory, _ = env
    dependency_pair(directory)
    prepare(manager, "test.provider", "test.consumer")
    assert all(record.instance is None for record in manager.records)
    assert manager.execute_command("test.consumer.run") == 42
    assert manager.get("test.provider").state.value == "active"
    assert manager.get("test.provider").activation_reason == "dependency:test.consumer"


def test_dependency_reload_rebuilds_active_consumers_and_uses_new_exports(env):
    manager, directory, _ = env
    provider, _ = dependency_pair(directory)
    prepare(manager, "test.provider", "test.consumer")
    assert manager.execute_command("test.consumer.run") == 42
    previous = manager.get("test.consumer").context
    (provider / "entry.py").write_text(PROVIDER.replace("41", "72"), encoding="utf-8")
    assert manager.reload("test.provider")
    assert manager.execute_command("test.consumer.run") == 73
    assert manager.get("test.consumer").context is not previous


def test_disabled_dependency_blocks_consumers_without_changing_their_enabled_intent(env):
    manager, directory, _ = env
    dependency_pair(directory)
    prepare(manager, "test.provider", "test.consumer")
    manager.execute_command("test.consumer.run")
    manager.disable("test.provider")
    record = manager.get("test.consumer")
    assert record.enabled
    assert record.state.value == "blocked"
    assert record.instance is None
    assert not manager.commands.has("test.consumer.run")
    assert manager.enable("test.provider")
    assert manager.execute_command("test.consumer.run") == 42


def test_missing_untrusted_or_incompatible_dependency_never_imports_consumer(env):
    manager, directory, _ = env
    provider, _ = dependency_pair(directory)
    prepare(manager, "test.consumer")
    record = manager.get("test.consumer")
    assert record.state.value == "blocked"
    assert record.instance is None
    manager.trust("test.provider")
    path = provider / "plugin.json"
    metadata = json.loads(path.read_text("utf-8"))
    metadata["version"] = "2.0.0"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    manager.reload("test.provider")
    assert manager.get("test.consumer").state.value == "blocked"
    assert "^1.0.0" in manager.get("test.consumer").error
    manager.uninstall("test.provider")
    assert manager.get("test.consumer").state.value == "blocked"
    assert "test.provider" in manager.get("test.consumer").error


def test_dependency_cycles_are_reported_without_importing_any_member(env):
    manager, directory, _ = env
    lazy_plugin(directory, "test.first", dependencies={"test.second": "*"})
    lazy_plugin(directory, "test.second", dependencies={"test.first": "*"})
    prepare(manager, "test.first", "test.second")
    assert all(record.state.value == "blocked" and record.instance is None for record in manager.records)
    assert "test.first" in manager.get("test.first").error
    assert "test.second" in manager.get("test.first").error


def test_provider_fault_tears_down_transitive_consumers_and_preserves_other_plugins(env):
    manager, directory, _ = env
    dependency_pair(directory)
    lazy_plugin(directory, "test.other")
    prepare(manager, "test.provider", "test.consumer", "test.other")
    manager.execute_command("test.consumer.run")
    manager.execute_command("test.other.run")
    other = manager.get("test.other").instance

    def fail():
        """在服务提供者命令中制造未处理错误。"""
        raise RuntimeError("provider failed")

    manager.get("test.provider").context.commands.register("test.provider.run", fail)
    manager.execute_command("test.provider.run")
    assert manager.get("test.provider").state.value == "error"
    assert manager.get("test.consumer").state.value == "blocked"
    assert manager.get("test.other").instance is other
    assert manager.execute_command("test.other.run") == 2
