#!/usr/bin/env bash
# 키트 파일을 현재 작업 폴더로 복사한다.
# 이미 있는 파일은 덮어쓰지 않는다. 네트워크, git, 설정은 건드리지 않는다.
kit="$(cd "$(dirname "$0")" && pwd)"
if [ "$kit" = "$(pwd)" ]; then
  echo "키트 폴더 안에서는 실행하지 않는다. 작업 폴더로 이동해 실행한다: bash $kit/apply.sh"
  exit 1
fi
failed=0
for f in AGENTS.md .gitignore make_data_guide.py trace_tools.py docs/PLAN.md docs/QUESTIONS.md docs/CAUSES.md docs/HOW-IT-WORKS.md docs/WORKLOG.md docs/STATUS.md docs/HANDOFF.md docs/data-guide/README.md docs/guide/data-agent.md docs/guide/docs-agent.md docs/guide/causes.md docs/guide/spc.md docs/guide/predict.md docs/guide/run.md; do
  if [ -e "$f" ]; then
    echo "건너뜀(이미 있음): $f"
  elif mkdir -p "$(dirname "$f")" && cp "$kit/$f" "$f"; then
    echo "복사함: $f"
  else
    echo "!!! 복사 실패: $f"
    failed=1
  fi
done
if [ -e make_data_guide.py ] && ! cmp -s make_data_guide.py "$kit/make_data_guide.py"; then
  echo "참고: 기존 make_data_guide.py를 그대로 두었다. 내용이 다르므로 AI가 확인한 뒤 사용해야 한다."
fi
if [ -e AGENTS.md ] && ! cmp -s AGENTS.md "$kit/AGENTS.md"; then
  echo "!!! 원래 있던 AGENTS.md를 그대로 두어서 키트 규칙이 적용되지 않았다."
  echo "    아래에 덧붙이려면: printf '\n' >> AGENTS.md && cat \"$kit/AGENTS.md\" >> AGENTS.md"
  echo "    덧붙인 뒤 wc -c AGENTS.md로 크기를 확인한다. Codex를 쓴다면 AGENTS.md들을 합쳐 32768바이트까지만 읽는다(키트 지침은 약 $(wc -c < "$kit/AGENTS.md")바이트)."
fi
if [ -e .gitignore ] && ! cmp -s .gitignore "$kit/.gitignore"; then
  echo "참고: 원래 있던 .gitignore를 그대로 두었다. 빠진 줄만 덧붙이려면: { printf '\n'; grep -vxFf .gitignore \"$kit/.gitignore\"; } >> .gitignore"
fi
echo "키트 버전: $(git -C "$kit" rev-parse --short HEAD 2>/dev/null || echo 알수없음)"
exit "$failed"
