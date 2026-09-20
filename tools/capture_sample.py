"""응답 표본 확보 도구(05 §4.9). 사용자가 직접 실행한다. 자동 테스트에서 실행하지 않는다.

이 도구는 어댑터를 직접 사용하는 명시적 예외다(브로커·작업 기록을 거치지 않고 아무것도 저장하지 않는다).
승인 문답을 거친 뒤 게임 채팅 1건을 보내고, 종료 코드·최상위 키 이름·status 값만 보여준다.
응답 원문은 출력하지 않는다. 확인한 값을 사용자가 config/known_responses.json에 직접 반영한다.

  python tools/capture_sample.py            # 성공 표본: 고정 문구 1건을 실제 게임 채팅으로 보낸다
  python tools/capture_sample.py --reject-sample   # 거부 표본: 게임이 처리할 수 없는 형식의 본문을 보낸다 [가정]
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobinogi_helper.cli_adapter import CliNotFound, adapter_for, locate_cli  # noqa: E402

SAMPLE_TEXT = "표본 확인 😊"


def ask(prompt: str) -> bool:
    try:
        return input(prompt).strip() in {"y", "Y"}
    except EOFError:
        return False


def summarize(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except ValueError:
        return "응답이 JSON이 아니에요."
    if not isinstance(data, dict):
        return f"응답이 객체가 아니에요(종류: {type(data).__name__})."
    status = data.get("status")
    status_text = f"status = {status!r}" if isinstance(status, str) else "status가 문자열이 아니에요"
    keys = ", ".join(sorted(str(key) for key in data.keys()))
    return f"최상위 키: [{keys}]\n{status_text}"


def main(argv: list) -> int:
    reject_mode = "--reject-sample" in argv
    if not sys.stdin.isatty():
        print("터미널에서 직접 실행해야 해요.")
        return 6
    try:
        adapter = adapter_for(locate_cli())
    except CliNotFound as error:
        print(f"오류: {error}")
        return 1

    if reject_mode:
        print("거부 표본: 게임이 처리할 수 없는 형식의 본문을 보내요. 채팅으로 전달되지 않을 것이라는 건 아직 [가정]이에요.")
        payload = "base64:!!!not-base64!!!"
        if not ask("게임 채팅창을 지켜보면서 진행할게요. 계속할까요? [y/N] "):
            print("취소했어요.")
            return 2
    else:
        print(f"성공 표본: 게임 채팅으로 실제로 '{SAMPLE_TEXT}'를 1건 보내요. 다른 플레이어에게 보여요.")
        payload = "base64:" + base64.b64encode(SAMPLE_TEXT.encode("utf-8")).decode("ascii")
        if not ask("정말 보낼까요? [y/N] "):
            print("취소했어요.")
            return 2

    raw = adapter.run("write_chat", [payload], timeout=15)
    print(f"종료 코드: {raw.exit_code}, 실패 사실: {raw.failure.value if raw.failure else '없음'}")
    print(summarize(raw.stdout))
    if not reject_mode:
        seen = ask("게임 채팅창에 그 말이 실제로 떴나요? [y/N] ")
        print(
            "떴다고 답했어요. 위 (종료 코드, status) 쌍을 성공 후보로 삼을 수 있어요."
            if seen
            else "뜨지 않았다고 답했어요. 이 status는 성공 목록에 넣지 마세요."
        )
    print("값을 확인하고 config/known_responses.json에 직접 반영하세요(config_verified를 true로).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
