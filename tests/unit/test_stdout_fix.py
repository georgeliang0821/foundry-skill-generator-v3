from __future__ import annotations

import pytest

from backend.patch import v4a_from_contents
from backend.skill_lint import script_only_errors, stdout_to_stderr

SCRIPT = '''"""Room lookup."""
import json
import requests

print("開始查詢會議室...")
response = requests.get("https://graph.example.invalid/rooms", timeout=10)
print(f"共 {len(response.json())} 筆")
print(json.dumps({"rooms": response.json()}))
'''


def _s4_lines(script: str) -> list[str]:
    return [issue.detail for issue in script_only_errors(script) if issue.rule == "S4"]


def test_progress_prints_move_to_stderr_and_sys_is_imported() -> None:
    fixed, lines = stdout_to_stderr(SCRIPT)

    assert lines == [5, 7]
    assert 'import sys\nimport json\n' in fixed
    assert 'print("開始查詢會議室...", file=sys.stderr)' in fixed
    assert 'print(f"共 {len(response.json())} 筆", file=sys.stderr)' in fixed
    assert 'print(json.dumps({"rooms": response.json()}))' in fixed
    assert _s4_lines(SCRIPT) and not _s4_lines(fixed)


def test_an_existing_import_sys_is_reused() -> None:
    fixed, _ = stdout_to_stderr("import sys\nprint('working')\n")

    assert fixed == "import sys\nprint('working', file=sys.stderr)\n"


def test_import_goes_after_the_docstring_and_future_imports() -> None:
    fixed, _ = stdout_to_stderr('"""Doc."""\nfrom __future__ import annotations\nprint()\n')

    assert fixed == '"""Doc."""\nfrom __future__ import annotations\nimport sys\nprint(file=sys.stderr)\n'


def test_multiline_call_with_trailing_comma() -> None:
    fixed, lines = stdout_to_stderr("import sys\nprint(\n    'a',\n    'b',\n)\n")

    assert lines == [2]
    assert fixed == "import sys\nprint(\n    'a',\n    'b', file=sys.stderr,\n)\n"


def test_crlf_is_kept() -> None:
    fixed, _ = stdout_to_stderr("x = 1\r\nprint('done')\r\n")

    assert fixed == "import sys\r\nx = 1\r\nprint('done', file=sys.stderr)\r\n"


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("result = {'a': 1}\nprint(result)\n", id="may-be-the-result"),
        pytest.param("print('Room', 'found:', name)\n", id="non-literal-argument"),
        pytest.param("print('Error: no room')\n", id="failure-word"),
        pytest.param("print('查詢失敗')\n", id="chinese-failure-word"),
        pytest.param("try:\n    x = 1\nexcept ValueError:\n    print('retrying')\n", id="inside-except"),
        pytest.param("import sys\nif x:\n    print('stopping')\n    sys.exit(3)\n", id="before-non-zero-exit"),
        pytest.param("print('a', end='')\n", id="keyword-argument"),
        pytest.param(
            "import json\nprint('[NEEDS_INFO] missing=ROOM')\nprint('ask the user')\nraise SystemExit(0)\n",
            id="after-needs-info",
        ),
    ],
)
def test_prints_that_need_judgement_are_left_alone(script: str) -> None:
    assert stdout_to_stderr(script) == (script, [])


def test_a_zero_exit_after_the_print_does_not_block_the_move() -> None:
    _, lines = stdout_to_stderr("import sys\nprint('bye')\nsys.exit(0)\n")

    assert lines == [2]


def test_invalid_python_raises() -> None:
    with pytest.raises(SyntaxError):
        stdout_to_stderr("print(\n")


def test_v4a_from_contents_shows_only_the_changed_lines_with_context() -> None:
    fixed, _ = stdout_to_stderr(SCRIPT)

    patch = v4a_from_contents("code-1", SCRIPT, fixed)

    assert patch.startswith("*** Begin Patch\n*** Update File: code-1\n@@\n")
    assert patch.endswith("\n*** End Patch")
    assert '-print("開始查詢會議室...")\n+print("開始查詢會議室...", file=sys.stderr)' in patch
    assert "+import sys" in patch
