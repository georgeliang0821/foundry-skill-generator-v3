"""
2026.08.21 George : Skill Registry 分階 —— 拓樸一致性守門

情境層(parent)與能力層(child)的綁定寫在 Blob SKILL.md 的 frontmatter,
沒有任何 SQL constraint 擋得住寫錯。最典型的失效是「安靜的」:parent 的
children 打錯一個字,child 就從此不可被發現,不會有錯誤訊息。這支測試把那類
錯誤提前到 CI。

注意:is_internal 存在 Azure SQL,本地測不到,所以這裡只驗證 SKILL.md 之間
的引用一致性(parent → child 指得到、error code 有被 parent 接住)。
"""
import re
import unittest
from pathlib import Path

import yaml

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _split_frontmatter(content: str) -> tuple:
    """回傳 (frontmatter_dict, body_str)。"""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            parsed = yaml.safe_load(content[3:end])
            return (parsed if isinstance(parsed, dict) else {}), content[end + 3:]
    return {}, content


def _load_skills() -> dict:
    """回傳 {skill_name: (frontmatter_dict, body_str)}。"""
    skills = {}
    if not SKILLS_ROOT.is_dir():
        return skills
    for skill_dir in sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        content = skill_md.read_text(encoding="utf-8")
        frontmatter, body = _split_frontmatter(content)
        skills[frontmatter.get("name") or skill_dir.name] = (frontmatter, body)
    return skills


def _metadata_list(frontmatter: dict, key: str) -> list:
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return []
    value = metadata.get(key)
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value] if isinstance(value, list) else []


_NEEDS_INFO_CODE = re.compile(r"missing=([A-Z][A-Z0-9_]*)")


def _needs_info_codes(body: str) -> set:
    """
    2026.08.22 George : 碼從 child 正文推導,不另立 frontmatter 欄位 ——
    手寫清單本身就會跟它描述的程式碼 drift。
    """
    return set(_NEEDS_INFO_CODE.findall(body))


class SkillTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skills = _load_skills()

    def test_declared_children_exist(self):
        for parent, (frontmatter, _) in self.skills.items():
            for child in _metadata_list(frontmatter, "children"):
                with self.subTest(parent=parent, child=child):
                    self.assertIn(
                        child, self.skills,
                        f"{parent} 宣告的 child '{child}' 不存在 —— "
                        f"該 child 將永遠不會被載入,且不會有任何錯誤訊息",
                    )

    def test_child_needs_info_codes_are_handled_by_parent(self):
        """
        child 的 `[NEEDS_INFO] missing=KEY` 會原樣進到 host 的 response,是兩層之間
        唯一可機器判讀的錯誤碼;parent 正文必須逐一寫出對應處理方式。
        其餘錯誤已在 child 內部譯成中文,host 看不到碼,不列入檢查。
        """
        for parent, (frontmatter, body) in self.skills.items():
            for child in _metadata_list(frontmatter, "children"):
                if child not in self.skills:
                    continue  # 由 test_declared_children_exist 負責報錯
                _, child_body = self.skills[child]
                for code in sorted(_needs_info_codes(child_body)):
                    with self.subTest(parent=parent, child=child, code=code):
                        self.assertIn(
                            code, body,
                            f"{child} 會回 '[NEEDS_INFO] missing={code}',但 parent "
                            f"{parent} 的正文沒有提到該如何處理",
                        )

    def test_a_skill_is_not_its_own_child(self):
        for parent, (frontmatter, _) in self.skills.items():
            with self.subTest(parent=parent):
                self.assertNotIn(parent, _metadata_list(frontmatter, "children"))


class ScenarioFilterDirectionTests(unittest.TestCase):
    """
    2026.08.22 George : v1.13 把情境層 parent 排除在 runtime materialize 之外。
    判準寫反(改成「被別人列為 child」)會擋掉真正要執行的能力層技能,
    而且是靜默失敗 —— 內部 agent 只會表現成「找不到技能」。這裡釘死方向。
    """

    def test_only_skills_declaring_children_are_filtered(self):
        try:
            from skills_provider_factory import _declares_children
        except Exception as e:  # noqa: BLE001 — 缺 azure 依賴時跳過而非誤報
            self.skipTest(f"skills_provider_factory 匯入失敗: {e}")

        skills = _load_skills()
        declared_children = {
            child
            for _, (frontmatter, _) in skills.items()
            for child in _metadata_list(frontmatter, "children")
        }

        parents, kids = 0, 0
        for skill_dir in sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            content = skill_md.read_text(encoding="utf-8")
            frontmatter, _ = _split_frontmatter(content)
            name = frontmatter.get("name") or skill_dir.name
            expected = bool(_metadata_list(frontmatter, "children"))
            parents += expected
            with self.subTest(skill=name):
                self.assertEqual(
                    expected, _declares_children(content),
                    f"{name}: materialize 過濾判斷跟 frontmatter 不一致",
                )
                if name in declared_children:
                    kids += 1
                    self.assertFalse(
                        _declares_children(content),
                        f"{name} 是能力層技能,被過濾掉後端就沒人執行了",
                    )

        self.assertGreater(parents, 0, "沒有任何情境層技能,本測試沒驗到東西")
        self.assertGreater(kids, 0, "沒有任何能力層技能,本測試沒驗到東西")


if __name__ == "__main__":
    unittest.main()
