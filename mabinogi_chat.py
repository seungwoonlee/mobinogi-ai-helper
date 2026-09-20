"""게임 채팅용 최소 대화형 명령어.

실행 후 ``@@ 보낼 문구``를 입력하면 해당 문구를 마비노기 모바일 채팅으로
전송한다. 게임 클라이언트의 AI 에이전트 연결이 켜져 있어야 한다.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_CLI_PATH = Path(r"C:\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe")
PREFIX = "@@"
MAX_CHAT_LENGTH = 50
EMOJI_SUFFIX_LENGTH = 2  # 공백 1자 + 단일 코드포인트 이모지 1자


@dataclass(frozen=True)
class ChatPlan:
    """전송할 대사와, 의도가 뚜렷할 때만 실행할 행동."""

    text: str
    behaviour: str | None


def build_chat_plan(message: str) -> ChatPlan:
    """문장 의도에 맞춰 이모지를 붙이고 행동을 선택한다.

    모든 대사에는 이모지를 붙인다. 행동은 뜻이 분명한 경우에만 붙여서,
    일반적인 대사에 불필요한 제스처가 나가지 않게 한다.
    """
    lowered = message.lower()
    rules = (
        (("미안", "죄송", "사과"), "😓", "/사과1"),
        (("축하", "ㅊㅋ"), "🥳", "/축하해"),
        (("고마", "감사"), "😍", "/하트"),
        (("사랑", "좋아해"), "😍", "/하트"),
        (("안녕", "반가", "어서"), "😊", "/손인사1"),
        (("잘 가", "잘가", "수고", "이만 갈", "이만갈"), "😉", "/손인사1"),
        (("화이팅", "힘내", "응원"), "🥳", "/응원댄스"),
        (("ㅋㅋ", "ㅎㅎ", "웃기", "재밌"), "🤣", "/웃기1"),
        (("슬프", "아쉽", "흑흑"), "😢", "/울기1"),
        (("최고", "대박", "짱", "신난"), "😎", "/최고"),
    )
    for keywords, emoji, behaviour in rules:
        if any(keyword in lowered for keyword in keywords):
            return ChatPlan(f"{message} {emoji}", behaviour)
    return ChatPlan(f"{message} 😊", None)


def cli_path() -> Path:
    """환경 변수로 재정의할 수 있는 게임 CLI 경로를 반환한다."""
    return Path(os.environ.get("MABINOGI_MOBILE_CLI", DEFAULT_CLI_PATH))


def parse_chat_input(line: str) -> str | None:
    """`@@ 메시지` 형식에서 메시지만 꺼낸다."""
    stripped = line.strip()
    if not stripped.startswith(PREFIX):
        return None

    message = stripped[len(PREFIX) :].strip()
    if not message:
        raise ValueError("@@ 뒤에 보낼 문구를 입력하세요.")
    if len(message) + EMOJI_SUFFIX_LENGTH > MAX_CHAT_LENGTH:
        raise ValueError(
            f"자동 이모지를 포함해 채팅은 {MAX_CHAT_LENGTH}자까지 보낼 수 있습니다. "
            f"문구는 {MAX_CHAT_LENGTH - EMOJI_SUFFIX_LENGTH}자까지 입력하세요."
        )
    if message.startswith(("/", "#")):
        raise ValueError("이 도구에서는 일반 대사만 보낼 수 있습니다.")
    return message


def send_chat(message: str, executable: Path | None = None) -> dict:
    """UTF-8 Base64 본문으로 채팅 또는 행동을 전송하고 JSON 결과를 돌려준다."""
    command_cli = executable or cli_path()
    if not command_cli.is_file():
        raise RuntimeError(f"게임 CLI를 찾을 수 없습니다: {command_cli}")

    payload = "base64:" + base64.b64encode(message.encode("utf-8")).decode("ascii")
    result = subprocess.run(
        [str(command_cli), "write_chat", payload],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stdout.strip() or result.stderr.strip() or "알 수 없는 오류"
        raise RuntimeError(f"게임에 연결하지 못했습니다 (종료 코드 {result.returncode}): {detail}")

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"게임 응답을 해석할 수 없습니다: {result.stdout.strip()}") from error

    body = response.get("body") if isinstance(response, dict) else None
    error_body = body if isinstance(body, dict) else response
    if response.get("status") in {"rejected", "invalid_body"} or error_body.get("error"):
        detail = error_body.get("message") or error_body.get("error") or "게임이 요청을 거부했습니다."
        raise RuntimeError(detail)
    return response


def send_decorated_chat(message: str, executable: Path | None = None) -> ChatPlan:
    """이모지가 붙은 대사를 보내고, 필요한 경우 이어서 행동을 실행한다."""
    plan = build_chat_plan(message)
    send_chat(plan.text, executable)
    if plan.behaviour:
        send_chat(plan.behaviour, executable)
    return plan


def run_interactive() -> int:
    print("게임 채팅 도구입니다. @@ 뒤에 문구를 입력하세요. 종료: exit 또는 quit")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            return 0

        if line.strip().lower() in {"exit", "quit"}:
            return 0

        try:
            message = parse_chat_input(line)
            if message is None:
                print("형식: @@ 보낼 문구")
                continue
            plan = send_decorated_chat(message)
            suffix = f" + {plan.behaviour}" if plan.behaviour else ""
            print(f"전송 완료: {plan.text}{suffix}")
        except (ValueError, RuntimeError) as error:
            print(f"오류: {error}")


def main(argv: list[str]) -> int:
    if argv:
        try:
            message = parse_chat_input(" ".join(argv))
            if message is None:
                raise ValueError("명령행에서도 @@ 보낼 문구 형식을 사용하세요.")
            plan = send_decorated_chat(message)
            suffix = f" + {plan.behaviour}" if plan.behaviour else ""
            print(f"전송 완료: {plan.text}{suffix}")
            return 0
        except (ValueError, RuntimeError) as error:
            print(f"오류: {error}", file=sys.stderr)
            return 1
    return run_interactive()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
