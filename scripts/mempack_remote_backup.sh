#!/usr/bin/env bash
# mempack_remote_backup.sh — 记忆包 age 加密 → GitHub 私有仓哑存储（personal 区专用）
#
# 用法: mempack_remote_backup.sh <pack.tar.gz> [--repo OWNER/REPO] [--key-file KEY] [--base BASE]
#   --repo      默认 zengyanze/mnevo-backup-personal
#   --key-file  age 私钥文件（读其中公钥做加密端），默认 ~/.mnevo/keys/personal.agekey
#   --base      仓库内目录前缀，默认 packs（最终路径 packs/<文件名>.age）
#
# 环境: 需要 age、gh（已登录）。恢复: gh api 下载 blob → base64 -d | age -d -i <私钥>
# 红线: 只用于 personal 区记忆包；work 区包不出公司基础设施（PLAN §9.2）。
set -euo pipefail

PACK="${1:?usage: mempack_remote_backup.sh <pack.tar.gz> [options]}"
shift || true
REPO="zengyanze/mnevo-backup-personal"
KEY_FILE="$HOME/.mnevo/keys/personal.agekey"
BASE="packs"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --key-file) KEY_FILE="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

command -v age >/dev/null || { echo "age not found"; exit 2; }
command -v gh  >/dev/null || { echo "gh not found";  exit 2; }
[ -f "$PACK" ] || { echo "pack not found: $PACK"; exit 2; }
[ -f "$KEY_FILE" ] || { echo "age key not found: $KEY_FILE (age-keygen -o 生成)"; exit 2; }

RECIPIENT=$(grep -o 'age1[0-9a-z]*' "$KEY_FILE" | head -1)
[ -n "$RECIPIENT" ] || { echo "no age1... public key in $KEY_FILE"; exit 2; }

NAME="$(basename "$PACK")"
REMOTE_PATH="${BASE}/${NAME}.age"

TMP_AGE="$(mktemp --suffix=.age)"
trap 'rm -f "$TMP_AGE"' EXIT
age -r "$RECIPIENT" -o "$TMP_AGE" "$PACK"
B64="$(base64 -w0 "$TMP_AGE")"
SIZE=$(wc -c < "$TMP_AGE")

# 注: 不带前导斜杠，防 git-bash 路径改写
gh api --method PUT "repos/${REPO}/contents/${REMOTE_PATH}" \
  -f message="backup: ${NAME} (${SIZE}B encrypted)" \
  -f content="$B64" > /dev/null

echo "backed up: ${PACK} -> ${REPO}/${REMOTE_PATH} (${SIZE} bytes encrypted)"
