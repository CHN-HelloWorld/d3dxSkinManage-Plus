# Mod 清单导出

本插件是**教学示例**，用于演示插件开发，也可实际使用。

将当前游戏的 Mod 信息保存为 CSV，便于核对启用状态、整理收藏或分享文字清单。需要宿主 1.0.0、SDK 2.0。

命令首次调用时激活。`entry.py` 在宿主线程读取快照和选择路径，`inventory.py` 通过 `ctx.tasks` 在后台完成计算及原子保存。重复请求复用正在执行的任务，停用后旧任务不再弹出通知。

## 使用

1. 在插件页启用「Mod 清单导出」。
2. 在插件详情的「操作」区或「插件命令」中执行「导出 Mod 清单」，选择 CSV 保存位置。
3. 从分类或角色的右键菜单执行同一命令时，只导出该节点及其子分类。
4. 在插件详情中勾选「仅导出已启用的 Mod」，可缩小下一次导出的范围。

CSV 固定使用 `game_id, mod_id, category, name, author, enabled, rating, tags` 表头，按分类路径、名称和 ID 排序。
`enabled` 为 `true` 或 `false`，`rating` 为原始分级代码，标签以分号连接。空范围仍导出表头。
文件使用带 BOM 的 UTF-8；逗号、引号和换行由 CSV 编码保留。可能被电子表格视为公式的文本会添加单引号前缀。
清单记录命令执行时的快照，不包含 Mod 本体、来源链接或可恢复的预设文件。

## 教学重点

- `activate()`：命令注册，以及一个命令如何复用于插件详情和分类菜单。
- `_export()`：处理没有游戏、过期分类、取消选择及写入错误。
- `build_inventory()`：只处理 SDK 快照；通过 `parent_id` 汇总分类子树，不访问宿主数据库。
- `write_inventory()`：在目标目录写入临时文件，检查取消后原子替换目标。
- 声明式设置使用 `ctx.configuration.get("enabled_only")`，支持宿主管理页修改和恢复默认；中文文案通过插件语言包翻译。

## 权限

`mods:read` 用于读取信息；`fs:write` 用于保存用户选择的 CSV；`storage` 保存筛选设置；
`ui:menus` 和 `ui:settings` 声明对应菜单和设置；命令自动出现在插件详情。插件不修改 Mod 或预设。

开发规范与完整接口说明见应用的 `docs/d3dxSkinManage-Plus 插件开发指南.html`；源码位于 `docs/tutorials/zh/插件开发指南.md`。
使用 `scripts/plugins/plugin_tool.py check` 校验，
再用 `pack --output <目录外的包.zip>` 按 `files` 生成 ZIP；务必包含 `inventory.py`。
自行分发衍生插件时应更换清单 ID 及贡献点中的命令 ID。`background` 用于宿主任务，`fs:write` 声明导出文件需求；
任务在提交临时文件前检查取消，已完成的原子替换不会被取消撤回。
