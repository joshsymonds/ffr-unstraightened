import pytest

from ffrd.pipeline.scope import in_scope, scoped
from ffrd.pipeline.stages import Scene, SceneLine

# Made-up namespaces only. Not game text.
# In scope with or without a scene table: the fallback allowlist.
IN_SCOPE: tuple[str, ...] = (
    "ST_C01_001_01",
    "ST_C12_003_07",
    "ST_C1_001",
    "ST_SubEv_004_C2_01",
    "ST_SubEv_004",
    "ST_SubEvent_001",
    "ST_ChrEv_001",
    "ST_FEV_001",
    "ST_LegendEV_001",
    "ST_GimEv_001",
    "ST_BtlEv_001",
    "ST_EV_001",
    "ST_DialogChoice",
    "ST_DialogChoice_001",
    "ST_MainSummary",
    "ST_MainSummary_002",
    "ST_UnitDescription",
    "ST_UnitDescription_003",
)

# Out without a scene table, and not excluded: the fallback allowlist does not list them.
OUT_OF_SCOPE: tuple[str, ...] = (
    "ST_Misc_001",
    "ST_Scene_C1x_001",
    "ST_Name_001",
    "ST_Menu_001",
    "ST_Item_001",
    "ST_Skill_001",
    "ST_Tutorial_001",
    "ST_C_001",
    "ST_Cx01_001",
    "ST_C01001",
    "ST_Chr_001",
    "ST_Ev_001",
    "ST_Fev_001",
    "ST_DialogChoiceX",
    "ST_DialogChoices_001",
    "ST_MainSummaryX",
    "ST_UnitDescriptionX",
    "XST_C01_001_01",
    "st_c01_001_01",
    "",
)

# Out without a scene table, in with one: not excluded, so the scene table decides.
SCENE_TABLE_ONLY: tuple[str, ...] = (
    "ST_Story_001",
    "ST_Misc_001",
    "ST_Scene_C1x_001",
    "ST_Name_001",
    "ST_Menu_001",
    "ST_Cx01_001",
)

# Out on both paths: hard exclusions, including excluded names under allowed prefixes.
EXCLUDED: tuple[str, ...] = (
    "ST_Tutorial_001",
    "ST_TutorialDialog_C01_001",
    "ST_UI_001",
    "ST_UIMenu_001",
    "ST_C01_001_Name",
    "ST_C01_001_SkillDescription",
    "ST_SubEv_004_Name",
    "ST_DialogChoice_Name",
    "ST_MainSummary_SkillDescription",
    "ST_UnitDescription_Name",
    "ST_SkillDescription",
    "ST_ConsumablesDescription_001",
    "ST_EquipmentDescription_001",
    "ST_ArtifactDescription_001",
    "ST_ConsumablesDescription_C01_001",
)


@pytest.mark.parametrize("namespace", IN_SCOPE)
def test_fallback_namespaces_are_in_scope_without_a_scene_table(namespace: str) -> None:
    assert in_scope(namespace, scene_table=False) is True


@pytest.mark.parametrize("namespace", IN_SCOPE)
def test_fallback_namespaces_are_in_scope_with_a_scene_table(namespace: str) -> None:
    assert in_scope(namespace, scene_table=True) is True


@pytest.mark.parametrize("namespace", OUT_OF_SCOPE)
def test_other_namespaces_are_out_of_scope_without_a_scene_table(namespace: str) -> None:
    assert in_scope(namespace, scene_table=False) is False


@pytest.mark.parametrize("namespace", SCENE_TABLE_ONLY)
def test_unlisted_namespaces_are_in_scope_only_with_a_scene_table(namespace: str) -> None:
    assert in_scope(namespace, scene_table=False) is False
    assert in_scope(namespace, scene_table=True) is True


@pytest.mark.parametrize("scene_table", [False, True])
@pytest.mark.parametrize("namespace", EXCLUDED)
def test_hard_exclusions_are_out_on_both_paths(namespace: str, scene_table: bool) -> None:
    assert in_scope(namespace, scene_table=scene_table) is False


def _line(namespace: str, key: str) -> SceneLine:
    # Not game text.
    return SceneLine(
        namespace=namespace,
        key=key,
        speaker=None,
        english="Not game text.",
        japanese="Not game text.",
        chapter=None,
    )


def test_scoped_keeps_in_scope_lines_in_order_under_the_same_key() -> None:
    scene = Scene(
        key="scene-a",
        lines=(
            _line("ST_C01_001_01", "a1"),
            _line("ST_Menu_001", "a2"),
            _line("ST_SubEv_004", "a3"),
            _line("ST_Item_001", "a4"),
            _line("ST_DialogChoice_002", "a5"),
        ),
    )

    assert scoped([scene], scene_table=False) == [
        Scene(
            key="scene-a",
            lines=(
                _line("ST_C01_001_01", "a1"),
                _line("ST_SubEv_004", "a3"),
                _line("ST_DialogChoice_002", "a5"),
            ),
        )
    ]


def test_scoped_drops_a_scene_with_no_in_scope_lines() -> None:
    scenes = [
        Scene(key="only-out", lines=(_line("ST_Skill_001", "b1"), _line("ST_Name_001", "b2"))),
        Scene(key="kept", lines=(_line("ST_C02_001_01", "c1"),)),
    ]

    assert scoped(scenes, scene_table=False) == [Scene(key="kept", lines=(_line("ST_C02_001_01", "c1"),))]


def test_scoped_keeps_scene_order() -> None:
    scenes = [
        Scene(key="third", lines=(_line("ST_C03_001_01", "d3"),)),
        Scene(key="first", lines=(_line("ST_C01_001_01", "d1"),)),
        Scene(key="second", lines=(_line("ST_MainSummary", "d2"),)),
    ]

    assert [scene.key for scene in scoped(scenes, scene_table=False)] == ["third", "first", "second"]


def test_scoped_returns_a_fully_in_scope_scene_unchanged() -> None:
    scene = Scene(
        key="all-in",
        lines=(_line("ST_C01_001_01", "e1"), _line("ST_UnitDescription_003", "e2")),
    )

    assert scoped([scene], scene_table=False) == [scene]


def test_scoped_keeps_unlisted_lines_only_when_the_scene_table_is_the_source() -> None:
    scene = Scene(
        key="mixed",
        lines=(
            _line("ST_Story_001", "f1"),
            _line("ST_C01_001_01", "f2"),
            _line("ST_Story_002_Name", "f3"),
        ),
    )

    assert scoped([scene], scene_table=False) == [Scene(key="mixed", lines=(_line("ST_C01_001_01", "f2"),))]
    assert scoped([scene], scene_table=True) == [Scene(key="mixed", lines=(_line("ST_Story_001", "f1"), _line("ST_C01_001_01", "f2")))]
