#!/usr/bin/env bash
# Launch a Claude Code session backed by GLM (Zhipu BigModel) — the 4th model
# family seat (Claude / GPT / GLM / DeepSeek).
#
# Credentials are injected into THIS SUBSHELL ONLY and are never exported
# globally: a global ANTHROPIC_BASE_URL would silently hijack the primary
# Claude Code session onto GLM.
#
#   scripts/glm_code.sh                        # interactive GLM session, cwd = here
#   scripts/glm_code.sh -p "review this diff"  # one-shot
#
# Roster (2026-07-21 user ruling; bumped 2026-08-16; **flash added 2026-09-05**):
# the GLM models in service are
#   glm-5.3        text flagship (2026-08-13, supersedes glm-5.2). Seat default.
#   glm-5.3-flash  light tier, same rank as deepseek-v4-flash (2026-09-05 user
#                  ruling) — and the ONLY GLM model here that can actually read
#                  an image (probed 6/6 on shape+colour questions, see
#                  AI_agent/logs/experiments/2026-09-05_model_roster_probe/).
# ⛔ glm-5v-turbo — the old vision pick — is now refused by the subscription:
#    {"error":{"code":"1311","message":"当前订阅套餐暂未开放GLM-5V-Turbo权限"}}
# ⛔⛔ glm-5.3 must NOT be given image work: it accepts the image block, returns
#    200, and then either claims it cannot see the picture or answers WRONG
#    (6 rectangles / yellow / blue on a 4-green fixture) — a SILENT degrade,
#    never an error. Route every image task to glm-5.3-flash:
#      GLM_MODEL=glm-5.3-flash scripts/glm_code.sh
# Every other GLM model — glm-5.2 / glm-5-turbo / glm-4.7 / glm-4.5-air /
# glm-4.6v — is OFF unless the user names it for that round. Hence the
# small/fast slot defaults to glm-5.3 rather than the cheaper glm-4.5-air;
# override per-run if the user asks:
#   GLM_MODEL=glm-5.2 scripts/glm_code.sh          # fall back to the old flagship
#   GLM_SMALL_MODEL=glm-5.3-flash scripts/glm_code.sh
#
# NOTE on glm-5.3: thinking is MANDATORY (`thinking.type:"disabled"` is accepted
# by the API but silently ignored — probed 2026-08-16, reasoning_tokens still
# billed) and `reasoning_effort` defaults to "max". Claude Code drives the
# thinking budget itself, so nothing to set here, but expect a heavier quota
# burn per turn than glm-5.2 gave (see AI_agent/workflow/models.md).
#
# Config lives in .env (gitignored): GLM_API_KEY / GLM_ANTHROPIC_BASE_URL.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
# shellcheck disable=SC1091
. "$REPO_ROOT/.env"
set +a

: "${GLM_API_KEY:?GLM_API_KEY missing from .env}"
: "${GLM_ANTHROPIC_BASE_URL:?GLM_ANTHROPIC_BASE_URL missing from .env}"

exec env -u ANTHROPIC_API_KEY \
  ANTHROPIC_BASE_URL="$GLM_ANTHROPIC_BASE_URL" \
  ANTHROPIC_AUTH_TOKEN="$GLM_API_KEY" \
  ANTHROPIC_MODEL="${GLM_MODEL:-glm-5.3}" \
  ANTHROPIC_SMALL_FAST_MODEL="${GLM_SMALL_MODEL:-glm-5.3}" \
  claude "$@"
