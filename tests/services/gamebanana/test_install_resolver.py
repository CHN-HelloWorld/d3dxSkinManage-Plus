"""InstallTargetResolver 测试

规则：只有「GameBanana 分类标签精确命中本地目录」才自动安装（置信度 1.0）；
标题/文件名的模糊分只用于给弹窗列表排序，永不自动安装。
"""

import pytest

from src.models.node import Node
from src.services.gamebanana.install_resolver import (
    FUZZY_CEILING,
    InstallTargetResolver,
)


@pytest.fixture
def characters():
    return [
        Node(id="c1", name="示例角色丑", name_en="Sigma Alpha"),
        Node(id="c2", name="示例角色寅", name_en="Sigma Beta"),
        Node(id="c3", name="示例角色丙", name_en="Gamma Unit"),
        Node(id="c4", name="示例角色卯", name_en="Omicron"),
        Node(id="c5", name="示例角色辰", name_en="Group d'Example"),
        Node(id="c6", name="SampleOmega", name_en=""),
    ]


def _resolve(r, characters, *, cat="", is_sub=True, title="Some Mod", fn="x.zip"):
    return r.resolve(title, fn, characters, category_name=cat, category_is_sub=is_sub)


# 分类标签精确命中 → 自动安装


def test_category_matches_name_en(characters):
    best, ranked = _resolve(
        InstallTargetResolver(),
        characters,
        cat="Group d'Example",
    )
    assert best is not None and best.id == "c5"
    assert ranked[0].node_id == "c5" and ranked[0].score == 1.0


def test_category_falls_back_to_name(characters):
    best, _ = _resolve(InstallTargetResolver(), characters, cat="SampleOmega")
    assert best is not None and best.id == "c6"


@pytest.mark.parametrize(
    "cat",
    [
        "group d'example",  # 大小写
        "Group d’Example",  # 弯撇号
        "Group  dExample",  # 撇号缺失 + 多空格
    ],
)
def test_category_normalization_still_exact(characters, cat):
    best, _ = _resolve(InstallTargetResolver(), characters, cat=cat)
    assert best is not None and best.id == "c5"


# 不是 1.0 就必须让用户选


def test_root_category_never_auto_matches(characters):
    chars = characters + [Node(id="c9", name="Character Skins")]
    best, _ = _resolve(
        InstallTargetResolver(),
        chars,
        cat="Character Skins",
        is_sub=False,
    )
    assert best is None


def test_duplicate_name_is_ambiguous(characters):
    chars = characters + [Node(id="c8", name="示例角色丙", name_en="Gamma Unit")]
    best, _ = _resolve(InstallTargetResolver(), chars, cat="Gamma Unit")
    assert best is None


def test_title_and_filename_never_auto_install(characters):
    best, ranked = _resolve(
        InstallTargetResolver(),
        characters,
        title="示例角色丙新皮肤",
        fn="gammaunit_skin.zip",
    )
    assert best is None
    assert ranked[0].node_id == "c3"
    assert ranked[0].score <= FUZZY_CEILING


def test_unknown_category_returns_none(characters):
    best, ranked = _resolve(
        InstallTargetResolver(),
        characters,
        cat="Nobody",
        title="randomstuff",
        fn="random.zip",
    )
    assert best is None
    assert len(ranked) >= len(characters)


# 候选列表形态


def test_no_others_fallback_candidate(characters):
    _, ranked = _resolve(InstallTargetResolver(), characters, cat="Gamma Unit")
    ids = {c.node_id for c in ranked}
    assert "__others__" not in ids
    assert len(ranked) == len(characters)


def test_candidates_carry_name_en(characters):
    _, ranked = _resolve(InstallTargetResolver(), characters, cat="Gamma Unit")
    by_id = {c.node_id: c for c in ranked}
    assert by_id["c3"].name_en == "Gamma Unit"
    assert by_id["c6"].name_en == ""


def test_empty_candidates_returns_none():
    best, ranked = InstallTargetResolver().resolve(
        "any",
        "any.zip",
        [],
        category_name="Gamma Unit",
        category_is_sub=True,
    )
    assert best is None
    assert ranked == []
