"""游戏页的常驻详情和配置面板。

未选择游戏时显示空状态，预览使用单图模式，保存、删除和激活由游戏页统一处理。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.game_service import GameConfig
from src.ui import icons, theme
from src.ui.widgets.preview import PreviewWidget
from src.utils.i18n import t as tr


class GameDetailPanel(QWidget):
    """右侧游戏详情/配置栏"""

    activate_requested = Signal(str)  # game_id（可能为空 = 未保存新游戏，GamePage 先存再激活）
    save_requested = Signal(dict)  # 收集的字段
    delete_requested = Signal(str)  # game_id
    preview_changed = Signal(str)  # 选定的预览图源路径
    exit_requested = Signal()  # 草稿退出信号，由游戏页决定是否需要确认。

    MIN_WIDTH = 300

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化游戏编辑、草稿和基线字段状态，构建详情区域后显示未选择提示。"""
        super().__init__(parent)
        self._game: GameConfig | None = None
        self._preview_path = ""
        self._draft = False  # 是否正在编辑尚未保存的草稿。
        self._baseline_fields: dict[str, str | bool] = {}  # 最近一次加载/保存成功时的字段快照
        # 复用 #previewPanel 亚克力规则（背景图模式下作磨砂面板，不再整块透明）
        self.setObjectName("previewPanel")
        # 自定义 QWidget 子类显式启用样式背景绘制。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(self.MIN_WIDTH)
        self._setup_ui()
        self.set_game(None)

    def _setup_ui(self) -> None:
        """构建游戏预览和路径表单，并分别连接保存、删除、激活及草稿确认退出动作。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        # page 0: 空状态
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch()
        tip = QLabel(tr("请选择游戏，查看相关配置"))
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 13px;")
        el.addWidget(tip)
        el.addStretch()
        self._stack.addWidget(empty)

        # page 1: 内容
        content = QWidget()
        c = QVBoxLayout(content)
        c.setContentsMargins(0, 0, 0, 0)
        c.setSpacing(8)

        self._title = QLabel("")
        self._title.setStyleSheet("font-weight: bold; font-size: 15px;")
        c.addWidget(self._title)

        self._preview = PreviewWidget()
        self._preview.set_single_image_mode(True)
        self._preview.set_empty_text(tr("拖拽 / 点击添加游戏预览图（仅一张）"))
        self._preview.setMinimumHeight(150)
        self._preview.image_dropped.connect(self._on_preview_set)
        # 单图模式下「删除」清空唯一预览图，并通知 GamePage 持久化。
        self._preview.delete_current_requested.connect(lambda _i=0: self._on_preview_cleared())
        c.addWidget(self._preview)

        form = QFormLayout()
        form.setSpacing(6)
        self._name = QLineEdit()
        form.addRow(tr("游戏名称"), self._name)
        self._name_en = QLineEdit()
        self._name_en.setToolTip(
            tr(
                "英文名用于到 GameBanana 查询，并作为 data/ 下的资源目录名；\n"
                "保存时若英文名改变，会自动重命名已绑定的资源目录（不会新建孤儿目录）。"
            )
        )
        form.addRow(tr("英文名称"), self._name_en)
        self._exe = QLineEdit()
        self._exe.setPlaceholderText(tr("程序路径，可附加启动参数"))
        self._exe.setToolTip(tr("可在程序路径后填写启动参数。路径含空格时，建议用双引号括起。"))
        form.addRow(tr("可执行文件"), self._browse_row(self._exe, self._browse_exe))
        self._migoto = QLineEdit()
        self._migoto.setToolTip(tr("3DMigoto 所在目录，仅用于启动 Mod 加载器。"))
        form.addRow(tr("3DMigoto"), self._browse_row(self._migoto, self._browse_migoto))
        self._mods = QLineEdit()
        self._mods.setToolTip(tr("已启用 Mod 的部署目录，须与 d3dx.ini 指定的 Mod 目录一致；未设置时无法启用 Mod。"))
        form.addRow(tr("Mod 文件夹"), self._browse_row(self._mods, self._browse_mods))
        c.addLayout(form)

        # 已保存游戏的按钮组（启用 / 保存 / 删除）
        self._activate_btn = QPushButton(tr("启用此游戏环境"))
        self._activate_btn.setProperty("accent", True)
        self._activate_btn.clicked.connect(self._on_activate)
        c.addWidget(self._activate_btn)

        btn_row = QHBoxLayout()
        self._save_btn = QPushButton(tr("保存"))
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn, 1)
        self._delete_btn = QPushButton(tr("删除"))
        self._delete_btn.setStyleSheet(
            f"QPushButton {{ color: {theme.color('danger')};"
            f" border: 1px solid {theme.color('danger')}; border-radius: 4px;"
            f" padding: 5px 10px; background-color: transparent; }}"
            f"QPushButton:hover {{ background-color: {theme.color('danger')}; color: white; }}"
        )
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._delete_btn, 1)
        self._saved_btn_row = QWidget()
        self._saved_btn_row.setLayout(btn_row)
        c.addWidget(self._saved_btn_row)

        # 草稿仅提供确认和退出，启用、保存及删除操作只针对已登记游戏。
        self._draft_btn_row = QWidget()
        draft_row = QHBoxLayout(self._draft_btn_row)
        draft_row.setContentsMargins(0, 0, 0, 0)
        draft_row.setSpacing(6)
        self._confirm_btn = QPushButton(tr("确认"))
        self._confirm_btn.setProperty("accent", True)
        self._confirm_btn.clicked.connect(self._on_save)  # 确认 = 保存草稿 → 添加成功
        draft_row.addWidget(self._confirm_btn, 1)
        self._exit_btn = QPushButton(tr("退出"))
        self._exit_btn.clicked.connect(lambda: self.exit_requested.emit())
        draft_row.addWidget(self._exit_btn, 1)
        c.addWidget(self._draft_btn_row)

        c.addStretch()
        self._stack.addWidget(content)

    def _browse_row(self, line_edit: QLineEdit, on_browse) -> QWidget:
        """将路径输入框与调用指定回调的文件浏览按钮组合成一行。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(line_edit, 1)
        b = QPushButton()
        icons.apply_icon(b, "actions/folder-open")
        b.setFixedWidth(36)
        b.setToolTip(tr("浏览"))
        b.clicked.connect(on_browse)
        h.addWidget(b)
        return w

    # 公开接口。
    def set_game(self, game: GameConfig | None, is_active: bool = False, draft: bool = False) -> None:
        """显示游戏详情；草稿模式仅提供确认和退出，保存流程由游戏页负责。"""
        self._game = game
        self._mods_path_selected = False
        self._draft = draft
        if game is None:
            self._baseline_fields = {}
            self._stack.setCurrentIndex(0)
            return
        self._stack.setCurrentIndex(1)
        title = tr("新游戏") if draft else (game.name or tr("新游戏"))
        self._title.setText(title + (tr("\u3000（当前环境）") if is_active else ""))
        self._name.setText(game.name or "")
        self._name_en.setText(game.name_en or "")
        self._exe.setText(game.exe_path or "")
        self._migoto.setText(game.migoto_path or "")
        self._mods.setText(game.mods_path or "")
        self._preview_path = game.preview or ""
        if self._preview_path and Path(self._preview_path).exists():
            self._preview.set_images([self._preview_path])
        else:
            self._preview.clear()
        # 根据草稿状态切换可用操作。
        self._saved_btn_row.setVisible(not draft)
        self._activate_btn.setVisible(not draft)
        self._draft_btn_row.setVisible(draft)
        if not draft:
            self._activate_btn.setEnabled(not is_active)
            self._activate_btn.setText(tr("当前环境") if is_active else tr("启用此游戏环境"))
            self._delete_btn.setEnabled(bool(game.id))  # 未保存的新游戏没有 id，不可删
        # 所有控件回填完成后记录基线；后续直接比较 collect，无需为每个输入框维护脏标志。
        self._baseline_fields = self.collect()

    def collect(self) -> dict:
        """收集经过首尾空白清理的游戏字段，以及预览路径和部署目录是否由用户选择的标志。"""
        return {
            "id": self._game.id if self._game else "",
            "name": self._name.text().strip(),
            "name_en": self._name_en.text().strip(),
            "exe_path": self._exe.text().strip(),
            "migoto_path": self._migoto.text().strip(),
            "mods_path": self._mods.text().strip(),
            "_mods_path_selected": self._mods_path_selected,
            "preview": self._preview_path,
        }

    def has_unsaved_input(self) -> bool:
        """检查草稿是否包含输入或预览，以决定退出时是否确认放弃。"""
        return bool(
            self._name.text().strip()
            or self._name_en.text().strip()
            or self._exe.text().strip()
            or self._migoto.text().strip()
            or self._mods.text().strip()
            or self._preview_path
        )

    def has_unsaved_changes(self) -> bool:
        """当前详情栏相对最近一次加载/保存是否有未保存修改。"""
        if self._game is None:
            return False
        if self._draft:
            return self.has_unsaved_input()
        return self._mods_path_selected or self.collect() != self._baseline_fields

    # 内部交互。
    def _on_preview_set(self, path: str) -> None:
        """保存新预览路径并发出预览变更信号，交由外部处理文件和配置持久化。"""
        self._preview_path = path
        self.preview_changed.emit(path)

    def _on_preview_cleared(self) -> None:
        """单图模式：移除/删除当前（唯一）预览图 → 清空显示并通知持久化清空。"""
        self._preview.clear()
        self._preview_path = ""
        if self._game is not None:
            self._game.preview = ""
        self.preview_changed.emit("")

    def sync_preview_path(self, path: str, *, persisted: bool = True) -> None:
        """回填预览路径，仅在持久化成功后同步游戏对象及已保存基线。"""
        self._preview_path = path
        if not persisted:
            return
        if self._game is not None:
            self._game.preview = path
        # 已保存游戏的预览图是即时持久化的，不应让它把表单误标为“未保存”。
        if self._baseline_fields:
            self._baseline_fields["preview"] = path

    def _on_save(self) -> None:
        # 名称校验和错误提示由游戏页统一处理。
        """收集当前表单字段并发出游戏保存请求。"""
        self.save_requested.emit(self.collect())

    def _on_delete(self) -> None:
        """仅对具有有效标识的已绑定游戏发出删除请求。"""
        if self._game and self._game.id:
            self.delete_requested.emit(self._game.id)

    def _on_activate(self) -> None:
        """发出当前游戏环境激活请求，未绑定游戏时传递空标识。"""
        self.activate_requested.emit(self._game.id if self._game else "")

    def _browse_exe(self) -> None:
        """选择游戏可执行文件，并在用户确认后填入路径输入框。"""
        p, _ = QFileDialog.getOpenFileName(self, tr("选择游戏可执行文件"), "", tr("可执行文件 (*.exe);;所有文件 (*)"))
        if p:
            self._exe.setText(p)

    def _browse_migoto(self) -> None:
        """选择 3DMigoto 所在目录并更新对应输入框。"""
        p = QFileDialog.getExistingDirectory(self, tr("选择 3DMigoto 目录"))
        if p:
            self._migoto.setText(p)

    def _browse_mods(self) -> None:
        """选择已启用 Mod 的部署目录，并将该路径标记为手动指定。"""
        p = QFileDialog.getExistingDirectory(self, tr("选择 Mod 文件夹"))
        if p:
            self._mods.setText(p)
            self._mods_path_selected = True
