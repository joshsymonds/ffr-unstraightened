import pytest

from ffrd.pipeline.scope import in_scope, scoped
from ffrd.pipeline.stages import Scene, SceneLine

# Made-up namespaces only. Not game text.
IN_SCOPE: tuple[str, ...] = (
    "ST_C01_001_01",
    "ST_C12_003_07",
    "ST_C1_001",
    "ST_SubEv_004_C2_01",
    "ST_SubEv_004",
    "ST_DialogChoice",
    "ST_DialogChoice_001",
    "ST_MainSummary",
    "ST_MainSummary_002",
    "ST_UnitDescription",
    "ST_UnitDescription_003",
)

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
    "ST_DialogChoiceX",
    "ST_MainSummaryX",
    "ST_UnitDescriptionX",
    "XST_C01_001_01",
    "st_c01_001_01",
    "ST_SubEvent_001",
    "",
)


@pytest.mark.parametrize("namespace", IN_SCOPE)
def test_in_scope_namespaces_are_in_scope(namespace: str) -> None:
    assert in_scope(namespace) is True


@pytest.mark.parametrize("namespace", OUT_OF_SCOPE)
def test_other_namespaces_are_out_of_scope(namespace: str) -> None:
    assert in_scope(namespace) is False


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

    assert scoped([scene]) == [
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

    assert scoped(scenes) == [Scene(key="kept", lines=(_line("ST_C02_001_01", "c1"),))]


def test_scoped_keeps_scene_order() -> None:
    scenes = [
        Scene(key="third", lines=(_line("ST_C03_001_01", "d3"),)),
        Scene(key="first", lines=(_line("ST_C01_001_01", "d1"),)),
        Scene(key="second", lines=(_line("ST_MainSummary", "d2"),)),
    ]

    assert [scene.key for scene in scoped(scenes)] == ["third", "first", "second"]


def test_scoped_returns_a_fully_in_scope_scene_unchanged() -> None:
    scene = Scene(
        key="all-in",
        lines=(_line("ST_C01_001_01", "e1"), _line("ST_UnitDescription_003", "e2")),
    )

    assert scoped([scene]) == [scene]
