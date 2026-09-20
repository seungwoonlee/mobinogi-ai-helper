"""추적표 메타 테스트: 2단계 범위의 시나리오 ID가 모두 존재하고 각 테스트에 단언이 있는지 검사한다.

시나리오의 충분성을 보증하지는 않는다(05 §4.10). 필수 ID 목록과 테스트 태그의 대응만 본다.
"""

import ast
from pathlib import Path
import unittest

REQUIRED = (
    ["C1", "C2", "C10", "C11", "C12", "C13", "C14", "C15", "C16", "C17", "C31", "C32", "C33", "C36", "G34"]
    + [f"T{n}" for n in list(range(1, 11)) + [17, 18, 19, 20, 22] + list(range(31, 37)) + list(range(40, 46))]
)


def _tagged_functions():
    found = {}
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            ids = []
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "tag":
                    ids += [arg.value for arg in decorator.args if isinstance(arg, ast.Constant)]
            if not ids:
                continue
            has_assert = any(
                (isinstance(inner, ast.Call) and getattr(inner.func, "attr", "").startswith("assert"))
                or isinstance(inner, ast.Assert)
                for inner in ast.walk(node)
            )
            for scenario_id in ids:
                found.setdefault(scenario_id, []).append((f"{path.name}::{node.name}", has_assert))
    return found


class TraceabilityTests(unittest.TestCase):
    def test_every_required_scenario_has_an_asserting_test(self):
        found = _tagged_functions()
        missing = [sid for sid in REQUIRED if sid not in found]
        self.assertEqual(missing, [], f"태그된 테스트가 없는 시나리오: {missing}")
        without_assert = [sid for sid in REQUIRED if not any(ok for _, ok in found[sid])]
        self.assertEqual(without_assert, [], f"단언이 없는 시나리오: {without_assert}")

    def test_production_code_has_no_broad_baseexception_handlers(self):
        """위반을 삼킬 수 있는 except BaseException·bare except를 프로덕션 코드에 두지 않는다."""
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in list((root / "mobinogi_helper").glob("*.py")) + [root / "mabinogi_chat.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    if node.type is None or (isinstance(node.type, ast.Name) and node.type.id == "BaseException"):
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
