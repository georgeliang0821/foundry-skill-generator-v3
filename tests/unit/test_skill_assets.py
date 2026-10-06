from __future__ import annotations

import hashlib
import json

import pytest

from backend.models import PERSIST_CONTEXT, Material, MaterialKind, Session, SkillAsset, SkillKind, Stage
from backend.session_store import LocalSessionStore
from backend.skill_assets import MAX_ASSET_CHARS, AssetRejected, check_asset, check_asset_set
from backend.skill_lint import lint_skill
from backend.state_machine import build_system_prompt, evaluate_skill_form


def _session_with_asset() -> Session:
    return Session(assets=[SkillAsset(path="assets/style.css", content="body{}")])


def test_api_dump_carries_metadata_but_not_content() -> None:
    dumped = _session_with_asset().model_dump(mode="json")

    assert dumped["assets"] == [
        {"path": "assets/style.css", "size": 6, "sha256": hashlib.sha256(b"body{}").hexdigest()}
    ]


def test_persist_context_keeps_content() -> None:
    dumped = json.loads(_session_with_asset().model_dump_json(context=PERSIST_CONTEXT))

    assert dumped["assets"][0]["content"] == "body{}"


def test_local_session_store_round_trips_asset_content(tmp_path) -> None:
    store = LocalSessionStore(root=tmp_path)
    session = _session_with_asset()

    store.save(session)

    assert store.load(session.id).assets[0].content == "body{}"


@pytest.mark.parametrize(
    ("path", "data", "reason"),
    [
        ("assets/logo.png", b"\x89PNG", "binary format"),
        ("assets/data.csv", b"\xff\xfe", "not valid UTF-8"),
        ("assets/data.txt", b"a\x00b", "NUL"),
        ("assets/big.txt", ("x" * (MAX_ASSET_CHARS + 1)).encode(), "limit is 20,000"),
        ("assets/sub/x.css", b"x", "subdirectories"),
        ("templates/x.css", b"x", "subdirectories"),
        ("assets/../x.css", b"x", "subdirectories"),
        ("assets\\x.css", b"x", "not `\\`"),
        ("assets/.env", b"x", "start with `.`"),
        ("references/SKILL.md", b"x", "cannot be an asset"),
        ("assets/a\tb.css", b"x", "control character"),
    ],
)
def test_check_asset_rejects_with_a_reason(path: str, data: bytes, reason: str) -> None:
    with pytest.raises(AssetRejected, match=reason.replace("\\", "\\\\").replace("(", r"\(")) as excinfo:
        check_asset(path, data)
    assert excinfo.value.path == path


def test_check_asset_returns_the_text_unchanged() -> None:
    assert check_asset("references/Notes File.md", "café\r\n".encode()) == "café\r\n"


def test_check_asset_set_limits_count_total_and_duplicate_names() -> None:
    session = Session()
    too_many = {f"assets/f{i}.txt": "x" for i in range(10)}
    assert any("limit is 9" in p for p in check_asset_set(session, too_many))

    too_big = {f"assets/f{i}.txt": "x" * 15_000 for i in range(3)}
    assert any("limit is 40,000" in p for p in check_asset_set(session, too_big))

    dup = {"assets/Style.css": "a", "references/style.CSS": "b"}
    assert any("unique across both folders" in p for p in check_asset_set(session, dup))

    assert check_asset_set(session, {"assets/a.css": "a", "references/b.md": "b"}) == []


@pytest.mark.parametrize(
    "session",
    [
        Session(skill_kind=SkillKind.SCENARIO),
        Session(skill_form="script"),
        Session(current_skill={"skill_md": "---\nname: s\nmetadata:\n  children: [a]\n---\n"}),
    ],
    ids=["scenario", "script", "children"],
)
def test_check_asset_set_refuses_skills_that_cannot_carry_assets(session: Session) -> None:
    assert check_asset_set(session, {"assets/a.css": "a"})
    assert check_asset_set(session, {}) == []


def test_assets_keep_a_new_skill_off_the_script_form() -> None:
    session = Session(
        materials=[Material(kind=MaterialKind.CODE, content="print(1)\n")],
        assets=[SkillAsset(path="assets/a.css", content="a")],
    )

    verdict = evaluate_skill_form(session)

    assert verdict.form == "inline"
    assert "assets" in [failure.check for failure in verdict.failures]


@pytest.mark.parametrize("stage", [Stage.DRAFT, Stage.REFINE, Stage.TEST])
def test_prompt_carries_the_asset_rules_and_full_text(stage: Stage) -> None:
    session = Session(current_stage=stage, assets=[SkillAsset(path="assets/style.css", content="body{color:red}")])

    prompt = build_system_prompt(session)

    assert "## Working With Skill Assets" in prompt
    assert "<<<BEGIN ASSET assets/style.css (15 chars)>>>\nbody{color:red}\n<<<END ASSET assets/style.css>>>" in prompt


def test_prompt_has_no_asset_text_without_assets() -> None:
    prompt = build_system_prompt(Session(current_stage=Stage.REFINE))

    assert "Skill Assets" not in prompt


def test_prepare_shows_assets_without_the_authoring_rules() -> None:
    prompt = build_system_prompt(_session_with_asset())

    assert "<<<BEGIN ASSET assets/style.css" in prompt
    assert "## Working With Skill Assets" not in prompt


_MD = "---\nname: demo\ndescription: d\n---\n\n## Overview\n\nBody.\n"


def _asset_issues(skill_md: str, paths: list[str]) -> list[tuple[str, str]]:
    issues = lint_skill(skill_md, SkillKind.CAPABILITY, asset_paths=paths)
    return [(issue.rule, issue.detail) for issue in issues if issue.rule in {"F2", "F3"}]


def test_f2_requires_each_asset_by_full_path_in_backticks() -> None:
    md = _MD + "\n## Skill Resources\n\n- `style.css` -- styles.\n"

    assert _asset_issues(md, ["assets/style.css"]) == [("F2", "assets/style.css")]
    assert _asset_issues(md.replace("`style.css`", "`assets/style.css`"), ["assets/style.css"]) == []


def test_f3_flags_paths_that_are_not_attached() -> None:
    md = _MD + (
        "\n## Skill Resources\n\n- `assets/style.css` -- styles.\n- `references/Notes File.md` -- notes.\n"
        'Call read_skill_resource(skill_name="demo", resource_name="assets/missing.html").\n'
        "Not mentions: https://x.test/assets/logo.png and src/assets/app.js.\n"
    )

    assert _asset_issues(md, ["assets/style.css", "references/Notes File.md"]) == [("F3", "assets/missing.html")]


def test_f2_f3_ignore_the_gatekeeper_addendum_and_skip_without_assets() -> None:
    md = _MD + "\n## Gatekeeper Addendum\n\n- `assets/style.css` and `assets/gone.css`.\n"

    assert _asset_issues(md, ["assets/style.css"]) == [("F2", "assets/style.css")]
    assert _asset_issues(md, []) == []
