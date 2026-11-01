import pytest

from ffrd.pipeline.scope import in_scope

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
