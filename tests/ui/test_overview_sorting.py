"""验证中英混合名称排序及类别与 Mod 的排序作用范围。"""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt

from src.models.mod import AgeRating, Mod, ModInfo, ModStatus
from src.ui.pages.overview import OverviewPage


@pytest.fixture(params=["grid", "list"])
def page(qtbot, temp_data_dir, request):
    """创建使用隔离配置的概览页，覆盖卡片和列表两种布局。"""
    result = OverviewPage()
    qtbot.addWidget(result)
    result.resize(1000, 650)
    result._set_view(request.param)
    yield result
    result.close()


def _select_sort(page, key):
    """通过界面入口切换排序，包含类别排序方向的状态更新。"""
    page._sort_combo.setCurrentIndex(page._sort_combo.findData(key))


def _assert_order(page, expected):
    """检查显示顺序与身份索引，列表布局同时检查实际行顺序。"""
    assert [item["id"] for item in page._display_items] == expected
    assert page._display_idx_map == {item_id: row for row, item_id in enumerate(expected)}
    if page._view_mode == "list":
        assert [
            page._table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(page._table.rowCount())
        ] == expected


def _set_mixed_content(page):
    """提供名称、导入时间、作者和分级顺序不同的同级条目。"""
    page.set_characters(
        [
            {"id": "cat-middle", "name": "Middle", "sort_order": 0},
            {"id": "cat-zulu", "name": "Zulu", "sort_order": 1, "node_type": "character"},
            {"id": "cat-alpha", "name": "Alpha", "sort_order": 2},
        ],
        refresh=False,
    )
    mods = [
        Mod(mod_id="z", name="Zulu", imported_at="2024-01-01", info=ModInfo(author="Alpha")),
        Mod(
            mod_id="a",
            name="Alpha",
            imported_at="2024-01-03",
            info=ModInfo(author="Zulu", age_rating=AgeRating.X),
            status=ModStatus.ENABLED,
        ),
        Mod(mod_id="b", name="Beta", imported_at="2024-01-02", info=ModInfo(age_rating=AgeRating.R)),
    ]
    page.set_current_mods(mods, list(mods))
    return mods


@pytest.mark.parametrize(
    ("sort_key", "indices"),
    [("alpha_asc", [1, 4, 3, 2, 0]), ("alpha_desc", [0, 2, 3, 4, 1])],
)
def test_alphabetical_sort_preserves_latin_prefixes_in_mixed_names(page, sort_key, indices):
    names = ["Z白色", "A蓝色", "blue", "白色2", "白色1"]
    page.set_characters(
        [{"id": f"cat-{i}", "name": name, "name_en": f"Alias {i}"} for i, name in enumerate(names)],
        refresh=False,
    )
    page.set_current_mods([Mod(mod_id=f"mod-{i}", name=name) for i, name in enumerate(names)])
    _select_sort(page, sort_key)

    _assert_order(page, [f"cat-{i}" for i in indices] + [f"mod-{i}" for i in indices])


@pytest.mark.parametrize(
    ("alpha_sort", "categories"),
    [
        ("alpha_asc", ["cat-alpha", "cat-middle", "cat-zulu"]),
        ("alpha_desc", ["cat-zulu", "cat-middle", "cat-alpha"]),
    ],
)
def test_mod_sorts_preserve_category_order_and_place_mods_first(page, alpha_sort, categories):
    _set_mixed_content(page)
    _select_sort(page, alpha_sort)
    for sort_key, expected_mods in (
        ("time_asc", ["z", "b", "a"]),
        ("time_desc", ["a", "b", "z"]),
        ("author_asc", ["z", "a", "b"]),
        ("author_desc", ["a", "z", "b"]),
        ("status_desc", ["a", "b", "z"]),
        ("age_asc", ["z", "b", "a"]),
        ("age_desc", ["a", "b", "z"]),
    ):
        _select_sort(page, sort_key)
        _assert_order(page, expected_mods + categories)


@pytest.mark.parametrize(
    ("sort_key", "expected_mods"),
    [
        ("time_asc", ["b", "a", "z"]),
        ("time_desc", ["z", "a", "b"]),
        ("author_asc", ["z", "a", "b"]),
        ("author_desc", ["a", "z", "b"]),
        ("status_desc", ["z", "a", "b"]),
        ("age_asc", ["z", "b", "a"]),
        ("age_desc", ["a", "b", "z"]),
    ],
)
def test_mod_delta_stays_before_categories_for_every_mod_sort(page, sort_key, expected_mods):
    mods = _set_mixed_content(page)
    _select_sort(page, "alpha_desc")
    _select_sort(page, sort_key)
    changed = replace(mods[0], name="Aardvark", imported_at="2024-01-04", status=ModStatus.ENABLED)

    assert page.apply_reconcile_delta([changed], ())

    _assert_order(page, expected_mods + ["cat-zulu", "cat-middle", "cat-alpha"])


def test_category_only_view_keeps_alphabetical_order_across_mod_sorts(page):
    _set_mixed_content(page)
    page.set_current_mods([])
    _select_sort(page, "alpha_desc")
    for sort_key in ("time_asc", "author_desc", "status_desc", "age_asc"):
        _select_sort(page, sort_key)
        _assert_order(page, ["cat-zulu", "cat-middle", "cat-alpha"])


def test_failed_load_restores_category_sort_before_refresh(page):
    _set_mixed_content(page)
    _select_sort(page, "alpha_desc")
    _select_sort(page, "time_asc")
    page.begin_loading()
    _select_sort(page, "alpha_asc")

    page.loading_failed()
    page._refresh_current_level()

    _assert_order(page, ["z", "b", "a", "cat-zulu", "cat-middle", "cat-alpha"])
