from dataclasses import replace
from datetime import datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from src.services.gamebanana.models import GbModRecord
from src.ui.widgets.gb_mod_card import GbModCard


def test_card_basic_display(qtbot, sample_record, fake_image_loader):
    c = GbModCard(sample_record, image_loader=fake_image_loader)
    qtbot.addWidget(c)
    assert c.title_label.text().startswith("Sample Mod")


def test_card_date_badge_only_shows_date_at_thumbnail_bottom_right(qtbot, sample_record, fake_image_loader):
    card = GbModCard(sample_record, image_loader=fake_image_loader, is_installed=True)
    qtbot.addWidget(card)
    card.show()
    expected = datetime.fromtimestamp(sample_record.date_updated_ts).strftime("%Y-%m-%d")
    badge = card.date_badge
    assert badge.text() == expected
    assert badge.toolTip() == datetime.fromtimestamp(sample_record.date_updated_ts).strftime("%Y-%m-%d %H:%M")
    assert not badge.isHidden()
    assert badge.x() + badge.width() == card.thumbnail.x() + card.thumbnail.width() - 6
    assert badge.y() + badge.height() == card.thumbnail.y() + card.thumbnail.height() - 6
    assert not badge.geometry().intersects(card.installed_badge.geometry())
    assert card.sizeHint().width() == 210 and card.sizeHint().height() == 270


@pytest.mark.parametrize("updated", [None, 0])
def test_card_date_uses_publication_when_update_is_missing(qtbot, fake_image_loader, updated):
    payload = {"_idRow": 101, "_sName": "Example Mod", "_tsDateAdded": 1700000000}
    if updated is not None:
        payload["_tsDateUpdated"] = updated
    record = GbModRecord.from_api(payload)
    card = GbModCard(record, image_loader=fake_image_loader)
    qtbot.addWidget(card)
    assert card.date_badge.text() == datetime.fromtimestamp(payload["_tsDateAdded"]).strftime("%Y-%m-%d")
    assert not card.date_badge.isHidden()


def test_card_date_rebind_updates_badge_and_render_cache(qtbot, sample_record, fake_image_loader):
    card = GbModCard(sample_record, image_loader=fake_image_loader, defer_image=True)
    qtbot.addWidget(card)
    badge = card.date_badge
    image = QPixmap(210, 160)
    image.fill(QColor("blue"))
    card.thumbnail.setPixmap(image)
    previous_key = card.scroll_render_key()
    assert previous_key is not None
    replacement = replace(sample_record, date_updated_ts=sample_record.date_updated_ts + 86400)
    card.set_record(replacement)
    card.thumbnail.setPixmap(image)
    assert card.date_badge is badge
    assert badge.text() == datetime.fromtimestamp(replacement.date_updated_ts).strftime("%Y-%m-%d")
    assert card.scroll_render_key() != previous_key


@pytest.mark.parametrize("timestamp", [0, -1, 10**30])
def test_card_invalid_date_never_shows_placeholder_or_epoch(qtbot, sample_record, fake_image_loader, timestamp):
    card = GbModCard(sample_record, image_loader=fake_image_loader)
    qtbot.addWidget(card)
    card.set_record(replace(sample_record, date_updated_ts=timestamp))
    assert card.date_badge.text() == ""
    assert card.date_badge.toolTip() == ""
    assert card.date_badge.isHidden()


class _CountingLoader:
    """统计 load 调用次数，用于校验缩略图延迟加载契约。"""

    def __init__(self):
        self.calls = 0

    def load(self, url, target, *, size=None, placeholder=None, crop_to_size=False):
        self.calls += 1

    def cancel_for(self, target):
        pass


def test_defer_image_skips_load_until_requested(qtbot, sample_record):
    """defer_image=True：构造时不发起缩略图加载；load_thumbnail 才加载，且幂等。

    这是 4K fling 顺滑的核心契约——虚拟滚动 fling 中只建占位卡，停稳后由 grid
    统一调 load_thumbnail，避免快速滚动时几百张卡同时发起图片加载致掉帧。"""
    loader = _CountingLoader()
    c = GbModCard(sample_record, image_loader=loader, defer_image=True)
    qtbot.addWidget(c)
    assert loader.calls == 0, "defer_image=True 不应在构造时加载缩略图"
    c.load_thumbnail()
    assert loader.calls == 1, "load_thumbnail() 应发起一次加载"
    c.load_thumbnail()
    assert loader.calls == 1, "load_thumbnail() 必须幂等，重复调用不重复加载"


def test_eager_image_loads_on_construction(qtbot, sample_record):
    loader = _CountingLoader()
    c = GbModCard(sample_record, image_loader=loader)
    qtbot.addWidget(c)
    assert loader.calls == 1


def test_card_emits_clicked(qtbot, sample_record, fake_image_loader):
    c = GbModCard(sample_record, image_loader=fake_image_loader)
    qtbot.addWidget(c)
    received = []
    c.clicked.connect(lambda rec: received.append(rec))
    qtbot.mouseClick(c, Qt.MouseButton.LeftButton)
    assert received and received[0].mod_id == sample_record.mod_id


def test_nsfw_blur_when_policy_blur(qtbot, sample_record, fake_image_loader):
    sample_record.is_nsfw = True
    c = GbModCard(sample_record, image_loader=fake_image_loader, nsfw_policy="blur")
    qtbot.addWidget(c)
    # NSFW 缩略图使用预先模糊的静态 pixmap，校验静态模糊状态。
    assert c.thumbnail._blur_on is True


def test_nsfw_no_blur_when_policy_show(qtbot, sample_record, fake_image_loader):
    sample_record.is_nsfw = True
    c = GbModCard(sample_record, image_loader=fake_image_loader, nsfw_policy="show")
    qtbot.addWidget(c)
    assert c.thumbnail._blur_on is False


def test_installed_badge_visible_when_installed(qtbot, sample_record, fake_image_loader):
    c = GbModCard(sample_record, image_loader=fake_image_loader, is_installed=True)
    qtbot.addWidget(c)
    c.show()
    qtbot.waitExposed(c)
    assert c.installed_badge.isVisible()


def test_installed_badge_hidden_when_not_installed(qtbot, sample_record, fake_image_loader):
    c = GbModCard(sample_record, image_loader=fake_image_loader, is_installed=False)
    qtbot.addWidget(c)
    assert c.installed_badge.isHidden() or not c.installed_badge.isVisible()


def test_type_badge_text(qtbot, sample_record, fake_image_loader):
    c = GbModCard(sample_record, image_loader=fake_image_loader)
    qtbot.addWidget(c)
    assert c.type_badge.text() == sample_record.model.value


def test_context_menu_has_copy_link(qtbot, sample_record, fake_image_loader):
    c = GbModCard(sample_record, image_loader=fake_image_loader)
    qtbot.addWidget(c)
    menu = c._build_context_menu()
    actions_text = [a.text() for a in menu.actions()]
    assert any("复制" in t or "链接" in t for t in actions_text)


def test_clipboard_on_copy_link(qtbot, sample_record, fake_image_loader):
    c = GbModCard(sample_record, image_loader=fake_image_loader)
    qtbot.addWidget(c)
    c._copy_link()
    cb = QApplication.clipboard()
    assert cb.text() == sample_record.page_url


# 连续进入和离开悬停状态后，卡片应恢复原始位置。

# 缩放动画以 _rest_geometry 为固定基准；多次悬停和离开后应恢复原始位置，避免累积偏移。


# 直接调 _animate_scale(target) 而不是 enterEvent —— Qt 的 QEnterEvent 构造
# 需要一堆 QPointF 参数，太重；hover 的真正语义是「触发 1.02 → 触发 1.0」，
# _animate_scale 才是被测函数。
def test_hover_then_leave_restores_position(qtbot, sample_record, fake_image_loader):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget

    host = QWidget()  # 给卡片一个父，避免 top-level 受窗口标题栏 inset 干扰
    host.resize(800, 600)
    c = GbModCard(sample_record, image_loader=fake_image_loader, parent=host)
    qtbot.addWidget(host)
    c.move(100, 200)
    host.show()
    qtbot.waitExposed(host)
    rest_before = QRect(c.pos(), c.size())

    c._animate_scale(1.02)  # 鼠标移入。
    qtbot.wait(200)  # 动画 150ms + 余量
    c._animate_scale(1.0)  # 鼠标移出。
    qtbot.wait(200)

    rest_after = QRect(c.pos(), c.size())
    assert rest_after == rest_before, f"leave 后位姿应回到 hover 前：before={rest_before} after={rest_after}"


def test_repeated_hover_does_not_drift(qtbot, sample_record, fake_image_loader):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    host.resize(800, 600)
    c = GbModCard(sample_record, image_loader=fake_image_loader, parent=host)
    qtbot.addWidget(host)
    c.move(50, 60)
    host.show()
    qtbot.waitExposed(host)
    rest_before = QRect(c.pos(), c.size())

    for _ in range(10):
        c._animate_scale(1.02)
        qtbot.wait(180)
        c._animate_scale(1.0)
        qtbot.wait(180)

    rest_after = QRect(c.pos(), c.size())
    assert rest_after == rest_before, (
        f"反复 hover 后漂移：before={rest_before} after={rest_after}; "
        f"delta=({rest_after.x() - rest_before.x()}, {rest_after.y() - rest_before.y()})"
    )


def test_layout_reflow_updates_rest_geometry(qtbot, sample_record, fake_image_loader):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    host.resize(800, 600)
    c = GbModCard(sample_record, image_loader=fake_image_loader, parent=host)
    qtbot.addWidget(host)
    c.move(10, 10)
    host.show()
    qtbot.waitExposed(host)

    # 父布局重新摆放
    c.move(300, 400)
    qtbot.wait(50)  # 给 moveEvent 派发的时间

    new_rest = QRect(c.pos(), c.size())
    # 此时 hover 一次，leave 后应回到 new_rest 而不是 (10,10)
    c._animate_scale(1.02)
    qtbot.wait(180)
    c._animate_scale(1.0)
    qtbot.wait(180)

    after = QRect(c.pos(), c.size())
    assert after == new_rest, f"reflow 后的新位置 ({new_rest}) 没成为锚点；leave 后停在 {after}"
