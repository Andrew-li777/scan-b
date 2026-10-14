#!/usr/bin/env bash
# scan-b 回归验证总入口
#
# 用法：
#   ./run_all.sh              # 跑「离线」组（无需服务，秒级）
#   ./run_all.sh --all        # 离线组 + 服务组（需服务在跑 + 凭据就位）
#   ./run_all.sh --e2e        # 额外开启慢速端到端（真实解析，可能数分钟）
#   SCAN_PYTHON=/path/to/venv/bin/python ./run_all.sh
#
# 退出码：0 = 全通过（含 SKIP）；1 = 有失败项。
set -u

cd "$(dirname "$0")" || exit 9

# ── 解释器：优先仓库 venv ──────────────────────────────────────
REPO_ROOT="$(cd ../.. && pwd)"
PY="${SCAN_PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in "$REPO_ROOT/venv/bin/python" "$REPO_ROOT/../venv/bin/python"; do
    [ -x "$cand" ] && PY="$cand" && break
  done
fi
[ -z "$PY" ] && PY="python3"

# ── 分组 ──────────────────────────────────────────────────────
OFFLINE=(verify_b2.py verify_a6.py verify_a1.py verify_a3a7.py verify_a5.py verify_b3.py)
SERVICE=(verify_a4.py verify_b1.py verify_b1b.py verify_b4.py)

RUN_SERVICE=0
E2E_ARGS=()
for a in "$@"; do
  case "$a" in
    --all) RUN_SERVICE=1 ;;
    --e2e) E2E_ARGS+=(--with-e2e) ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $a（可用：--all --e2e --help）"; exit 2 ;;
  esac
done

TARGETS=("${OFFLINE[@]}")
[ "$RUN_SERVICE" -eq 1 ] && TARGETS+=("${SERVICE[@]}")

echo "解释器 : $PY"
echo "仓库根 : $REPO_ROOT"
echo "用例数 : ${#TARGETS[@]}"
echo

pass=0; fail=0; skipped=0; failed_list=""

for f in "${TARGETS[@]}"; do
  echo "───────────────────────────────────────────── $f"
  args=()
  if [ "$f" = "verify_b4.py" ] && [ ${#E2E_ARGS[@]} -gt 0 ]; then args=("${E2E_ARGS[@]}"); fi

  out=$("$PY" "$f" "${args[@]+"${args[@]}"}" 2>&1)
  rc=$?
  echo "$out" | sed 's/^/  /'

  if echo "$out" | grep -q '^\[SKIP\]'; then
    skipped=$((skipped + 1))
    echo "  => SKIP"
  elif [ $rc -eq 0 ]; then
    pass=$((pass + 1))
    echo "  => PASS"
  else
    fail=$((fail + 1))
    failed_list="$failed_list $f"
    echo "  => FAIL (exit=$rc)"
  fi
  echo
done

echo "═════════════════════════════════════════════"
echo "通过 $pass / 失败 $fail / 跳过 $skipped"
[ -n "$failed_list" ] && echo "失败用例:$failed_list"
[ "$fail" -eq 0 ] || exit 1
exit 0
