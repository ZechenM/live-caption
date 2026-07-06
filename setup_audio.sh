#!/usr/bin/env bash
# 一键切换系统输出到 Multi-Output Device, 退出时恢复原设备。
# 依赖: brew install switchaudio-osx
#
# 用法:
#   ./setup_audio.sh on  [设备名]   切到 Multi-Output (默认名取自 config.yaml)
#   ./setup_audio.sh off            恢复之前的输出设备
#   ./setup_audio.sh run            切换 -> 启动字幕程序 -> 退出时自动恢复
#   ./setup_audio.sh list           列出所有输出设备

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="${DIR}/.prev_output_device"

if ! command -v SwitchAudioSource >/dev/null 2>&1; then
  echo "未安装 switchaudio-osx, 请先执行: brew install switchaudio-osx" >&2
  exit 1
fi

# 从 config.yaml 读默认 Multi-Output 名称
default_multi() {
  grep -E '^\s*multi_output_name:' "${DIR}/config.yaml" \
    | sed -E 's/.*multi_output_name:[[:space:]]*"?([^"#]*[^"# ])"?.*/\1/'
}

# 在 config.yaml 的 device_map 里查当前设备对应的 Multi-Output 名称。
# device_map 的条目格式必须是:  "设备名": "Multi-Output名"
map_lookup() {
  local target="$1"
  sed -nE 's/^[[:space:]]+"(.+)"[[:space:]]*:[[:space:]]*"(.+)".*/\1\t\2/p' "${DIR}/config.yaml" \
    | while IFS=$'\t' read -r k v; do
        if [[ "${k}" == "${target}" ]]; then
          echo "${v}"
          return
        fi
      done
}

switch_on() {
  local cur mapped multi
  cur="$(SwitchAudioSource -c -t output)"
  if [[ -n "${1:-}" ]]; then
    multi="$1"                       # 命令行显式指定, 优先级最高
  else
    # 当前输出已包含 BlackHole (如某个 Multi-Output 设备) 则不硬切
    shopt -s nocasematch
    if [[ "${cur}" == *blackhole* || "${cur}" == *multi-output* || "${cur}" == *-bh* ]]; then
      shopt -u nocasematch
      echo "当前输出 [${cur}] 已包含 BlackHole, 保持不变。"
      return
    fi
    shopt -u nocasematch
    # 按 config.yaml 的 device_map 自动选择对应的 Multi-Output
    mapped="$(map_lookup "${cur}")"
    if [[ -n "${mapped}" ]]; then
      multi="${mapped}"
      echo "根据 device_map: [${cur}] -> [${multi}]"
    else
      multi="$(default_multi)"
      echo "device_map 中没有 [${cur}], 使用默认: ${multi}"
    fi
  fi
  if [[ "${cur}" == "${multi}" ]]; then
    echo "系统输出已经是 ${multi}, 无需切换。"
    return
  fi
  echo "${cur}" > "${STATE_FILE}"
  if SwitchAudioSource -t output -s "${multi}"; then
    echo "已切换: ${cur} -> ${multi}"
    echo "提示: Multi-Output 设备下音量键会失效, 调音量方法见 README。"
  else
    rm -f "${STATE_FILE}"
    echo "切换失败: 找不到设备 [${multi}]。" >&2
    echo "请先在音频MIDI设置中创建它 (见 README), 或用 ./setup_audio.sh list 查看设备名。" >&2
    exit 1
  fi
}

switch_off() {
  if [[ ! -f "${STATE_FILE}" ]]; then
    echo "没有记录之前的输出设备, 无需恢复。"
    return
  fi
  local prev
  prev="$(cat "${STATE_FILE}")"
  if SwitchAudioSource -t output -s "${prev}"; then
    echo "已恢复输出设备: ${prev}"
  else
    echo "恢复失败: 设备 [${prev}] 不可用 (可能耳机已断开), 请在系统设置中手动选择。" >&2
  fi
  rm -f "${STATE_FILE}"
}

case "${1:-}" in
  on)   switch_on "${2:-}" ;;
  off)  switch_off ;;
  list) SwitchAudioSource -a -t output ;;
  run)
    switch_on "${2:-}"
    trap switch_off EXIT INT TERM
    cd "${DIR}"
    if [[ -x "venv/bin/python" ]]; then venv/bin/python main.py
    else python3 main.py; fi
    ;;
  *)
    echo "用法: $0 on|off|run|list [Multi-Output 设备名]"
    exit 1
    ;;
esac
