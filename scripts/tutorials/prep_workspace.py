"""生成不依赖个人资源的教程演示库；只写带专用标记的系统临时目录。"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from demo_common import DEMO_VERSION, GAME_ID, REPO, isolate


def make_preview(destination: Path, title: str, color: str) -> None:
    """生成只有几何图案和说明文字的占位图，不使用任何游戏或角色素材。"""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (800, 600), "#16222e")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((28, 28, 772, 572), radius=28, fill=color)
    draw.rounded_rectangle((54, 430, 746, 548), radius=18, fill="#16222e")
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    title_font = ImageFont.truetype(str(font_path), 32)
    caption_font = ImageFont.truetype(str(font_path), 18)
    draw.rounded_rectangle((268, 106, 532, 316), radius=16, outline="#e0ebef", width=6)
    draw.ellipse((450, 136, 486, 172), fill="#e0ebef")
    draw.line(((287, 291), (354, 209), (411, 263), (455, 223), (513, 291)), fill="#e0ebef", width=7)
    draw.text((324, 352), "预览图占位", fill="#e0ebef", font=title_font)
    draw.text((80, 452), title, fill="white", font=title_font)
    draw.text((82, 503), "教程占位数据 · SAMPLE", fill="#b8c7d4", font=caption_font)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)


def create_demo() -> dict:
    """通过当前管理器导入自制归档并建立真实分类、预设和变体记录。"""
    workspace = isolate(create=True)
    # 演示环境使用随程序提供的语言包，确保中英文截图都来自真实翻译。
    shutil.copytree(REPO / "config/languages", workspace / "config/languages", dirs_exist_ok=True)
    manifest = workspace / "demo.json"
    if manifest.is_file():
        existing = json.loads(manifest.read_text(encoding="utf-8"))
        if existing.get("version") != DEMO_VERSION:
            raise RuntimeError("演示数据版本不同，请为旧工作区另行备份后再创建。")
        print(f"复用演示数据：{workspace}")
        return existing

    from src.core.config import Config
    from src.core.game_config_store import GameConfigStore
    from src.core.game_service import GameConfig, GameService
    from src.core.mod_manager import ModManager

    config = Config()
    for key, value in {
        "general.theme": "dark",
        "general.language": "zh-CN",
        "import.format": "zip",
        "import.strict_format": False,
        "gamebanana.enabled": False,
        "gamebanana.update_check.enabled": False,
        "gamebanana.update_check.on_startup": False,
        "app_update.on_startup": False,
        "ui.overview_card_scale": 100,
        "ui.overview_preview_width": 390,
        "ui.overview_preview_split": [310, 430],
        "ui.game_detail_width": 415,
    }.items():
        config.set(key, value)
    game_preview = workspace / "demo/game.png"
    make_preview(game_preview, "示例游戏", "#276386")
    migoto = workspace / "demo/3DMigoto"
    deploy = migoto / "Mods"
    deploy.mkdir(parents=True, exist_ok=True)
    (migoto / "d3dx.ini").write_text(
        "[Include]\ninclude_recursive = Mods\nexclude_recursive = DISABLED*\n", encoding="utf-8"
    )
    service = GameService()
    gid = service.save_game(
        GameConfig(
            name="示例游戏",
            name_en=GAME_ID,
            exe_path=str(workspace / "demo/Game/SampleGame.exe"),
            migoto_path=str(migoto),
            mods_path=str(deploy),
            preview=str(game_preview),
        )
    )
    GameConfigStore().set_last_game_id(gid)
    service.current_game_id = gid
    manager = ModManager()
    manager.set_game(gid)
    manager.set_deploy_dir(str(deploy))
    nodes = {}
    for name, english, kind in (
        ("外观", "Appearance", "category"),
        ("界面", "Interface", "category"),
        ("待整理", "Inbox", "category"),
        ("示例角色", "Sample Character", "character"),
    ):
        node = manager.add_character(name, english, game_id=gid, node_type=kind)
        if node is None:
            raise RuntimeError(f"无法创建演示分类：{name}")
        nodes[name] = node.id

    mods = []
    variants = []
    samples = (
        ("示例 Mod A", "#327e8b", "外观", ["配色", "日常"]),
        ("示例 Mod B", "#735385", "外观", ["配色", "摄影"]),
        ("示例 Mod C", "#a37044", "外观", ["日常", "简洁"]),
        ("示例 Mod D", "#386c58", "外观", ["配件"]),
        ("示例界面 Mod", "#375d91", "界面", ["简洁", "界面"]),
        ("示例角色 Mod", "#914d65", "示例角色", ["配色"]),
    )
    downloads = workspace / "demo/待导入"
    downloads.mkdir(parents=True, exist_ok=True)
    for i, (name, color, category, tags) in enumerate(samples):
        preview = workspace / "demo/previews" / f"{name}.png"
        make_preview(preview, name, color)
        package = downloads / f"{name}.zip"
        ini = (
            f"; {name}\nnamespace = tutorial_sample_{i}\n"
            "[Constants]\nglobal persist $style = 0\n"
            "[KeyStyle]\nkey = k\nback = j\ntype = cycle\n$style = 0,1,2\n"
        )
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("sample.ini", ini)
            archive.write(preview, "preview.png")
            archive.writestr("README.txt", "本文件仅用于教程截图，不包含真实游戏资源。")
        mod = manager.import_mod(str(package), node_id=nodes[category], name=name)
        if mod is None:
            raise RuntimeError(f"无法导入演示 Mod：{name}")
        mod.info.author = "教程示例"
        mod.info.description = "演示导入、标签、预览和变体管理。此示例不用于真实游戏。"
        mod.info.version = "1.0"
        mod.info.tags = tags
        if not manager.update_mod(mod):
            raise RuntimeError(f"无法保存演示信息：{name}")
        mods.append(mod.mod_id)

    enabled = manager.toggle_mod(mods[0])
    if enabled is None or not enabled.is_enabled:
        raise RuntimeError("演示 Mod 部署失败。")
    for name, value in (("外观方案 A", "1"), ("外观方案 B", "2")):
        (migoto / "d3dx_user.ini").write_text(
            "[Constants]\n$\\tutorial_sample_0\\style = " + value + "\n", encoding="utf-8"
        )
        variants.append(manager.variants.save_variant(mods[0], name).variant_id)
    current = manager.preset_manager.get_current(gid)
    manager.preset_manager.create_preset(gid, "日常搭配", current.character_mods, copied_from=current.id)
    manager.preset_manager.create_preset(gid, "摄影搭配", {nodes["外观"]: [mods[1]]})
    manager.preset_manager.create_preset(gid, "空白搭配")

    plugin_state = {}
    for plugin_id in ("example.mod-inventory", "example.preset-audit", "example.preset-cycle"):
        source = REPO / "plugins" / plugin_id
        plugin_manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
        shutil.copytree(source, workspace / "plugins" / plugin_id, dirs_exist_ok=True)
        plugin_state[plugin_id] = {
            "enabled": False,
            "trusted": False,
            "version": plugin_manifest["version"],
            "granted_permissions": [],
        }
    (workspace / "config/plugin_state.json").write_text(
        json.dumps(plugin_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    payload = {"version": DEMO_VERSION, "game_id": gid, "nodes": nodes, "mods": mods, "variants": variants}
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manager.stop_ini_sync()
    manager.variants.stop_watch()
    print(f"演示数据已创建：{workspace}；{len(nodes)} 个分类，{len(mods)} 个 Mod，{len(variants)} 个变体。")
    return payload


if __name__ == "__main__":
    create_demo()
