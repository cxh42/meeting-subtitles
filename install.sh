#!/usr/bin/env bash
# Install 会议字幕 / Meeting Subtitles for the current user.
# The lockfile is the only dependency source, so `uv run` cannot replace the
# CUDA 12 libraries or the engine's glossary-capable commit after installation.

set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"

VENV="$REPO/.venv"
PYTHON="$VENV/bin/python"
PYTHON_VERSION="$(cat "$REPO/.python-version")"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
DESKTOP="$APPS/meeting-subtitles.desktop"
ICON="$ICONS/meeting-subtitles.svg"

usage() {
    cat <<'EOF'
用法：./install.sh [选项]

  默认              用 uv 和 uv.lock 安装 Python 3.12、CUDA 12.8 依赖与桌面入口
  --cuda cu128      显式选择锁定的 CUDA 12.8 构建
  --no-desktop      跳过桌面入口
  --download-models 同时下载全部默认模型，之后可离线启动
  --remove          移除桌面入口，保留 .venv 和模型
  -h, --help        显示帮助

需要联网下载依赖时，可先设置：
  export http_proxy=http://127.0.0.1:7897
  export https_proxy=http://127.0.0.1:7897
  export no_proxy=localhost,127.0.0.1,::1
EOF
}

WANT_DESKTOP=1
DOWNLOAD_MODELS=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --cuda)
            if [ "$#" -lt 2 ]; then
                echo "--cuda 缺少参数；请使用 --cuda cu128。" >&2
                exit 2
            fi
            if [ "$2" != "cu128" ]; then
                echo "当前 uv.lock 固定使用 CUDA 12.8；请使用 --cuda cu128，或修改 pyproject.toml 的 PyTorch 源后运行 uv lock。" >&2
                exit 2
            fi
            shift 2 ;;
        --cuda=cu128) shift ;;
        --cuda=*|--cpu|--pypi-engine)
            echo "此选项与锁定部署不兼容。请直接运行 ./install.sh；如需其他构建，请修改 pyproject.toml 的依赖源并运行 uv lock。" >&2
            exit 2 ;;
        --no-desktop) WANT_DESKTOP=0; shift ;;
        --download-models) DOWNLOAD_MODELS=1; shift ;;
        --remove)
            rm -f "$DESKTOP" "$ICON"
            update-desktop-database "$APPS" 2>/dev/null || true
            gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
            echo "已移除桌面入口。虚拟环境 $VENV 未删除。"
            exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1。运行 ./install.sh --help 查看用法。" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[36m==\033[0m %s\n' "$1"; }
note() { printf '   \033[2m%s\033[0m\n' "$1"; }

step "检查系统依赖"
if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
    echo "当前锁定部署支持 Linux x86_64；请在此平台运行安装脚本。" >&2
    exit 1
fi
MISSING=()
command -v ffmpeg >/dev/null || MISSING+=(ffmpeg)
command -v pactl >/dev/null || MISSING+=(pulseaudio-utils)
command -v git >/dev/null || MISSING+=(git)
command -v cc >/dev/null || MISSING+=(build-essential)
command -v fc-list >/dev/null || MISSING+=(fontconfig)
# Matching captured output avoids SIGPIPE under pipefail when grep exits early.
FONTS="$(fc-list 2>/dev/null || true)"
grep -qi "noto sans cjk\|wenquanyi\|source han\|noto sans sc" <<<"$FONTS" \
    || MISSING+=(fonts-noto-cjk)
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "缺少系统包：${MISSING[*]}"
    echo "请先运行：sudo apt update && sudo apt install -y ${MISSING[*]}"
    echo "装好后重新运行 ./install.sh。"
    exit 1
fi
note "ffmpeg / pactl / git / C 编译器 / 中文字体已就绪"

step "检查 uv"
if command -v uv >/dev/null; then
    UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
else
    if ! command -v curl >/dev/null; then
        echo "安装 uv 需要 curl；请先运行 sudo apt install -y curl，再运行 ./install.sh。" >&2
        exit 1
    fi
    note "从 Astral 官方地址安装 uv 到当前用户目录"
    UV_INSTALLER="$(mktemp)"
    trap 'rm -f "$UV_INSTALLER"' EXIT
    curl --fail --location --silent --show-error https://astral.sh/uv/install.sh -o "$UV_INSTALLER"
    UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$UV_INSTALLER"
    UV="$HOME/.local/bin/uv"
fi
note "$("$UV" --version)"

step "检查 Python $PYTHON_VERSION 与 Tk"
# The engine excludes Python 3.14. uv supplies the pinned interpreter when the
# distribution only ships a newer one, without changing the system Python.
# Snap-hosted terminals redirect XDG_DATA_HOME into a revision directory that
# may disappear on upgrade; the desktop entry needs a stable interpreter.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
BASE_PYTHON="$("$UV" python find --managed-python "$PYTHON_VERSION" 2>/dev/null || true)"
if [ -z "$BASE_PYTHON" ]; then
    "$UV" python install "$PYTHON_VERSION"
    BASE_PYTHON="$("$UV" python find --managed-python "$PYTHON_VERSION")"
fi
if ! "$BASE_PYTHON" -c 'import tkinter' 2>/dev/null; then
    echo "Python $PYTHON_VERSION 无法加载 Tk。请运行 uv python install --reinstall $PYTHON_VERSION 后重试。" >&2
    exit 1
fi
"$BASE_PYTHON" - <<'PY'
import os
import tkinter as tk
from meeting_subtitles.tkfix import cjk_families, system_cjk_families, system_tk_environment

if os.environ.get("DISPLAY"):
    if not cjk_families() and not system_cjk_families(system_tk_environment()):
        raise SystemExit(f"Tk 无法显示中文。请运行 sudo apt install libtk{tk.TkVersion} fonts-noto-cjk，再重新运行 ./install.sh。")
PY
note "$("$BASE_PYTHON" --version)；Tk 可加载"

step "按 uv.lock 同步项目依赖（CUDA 12.8）"
"$UV" sync --locked --python "$BASE_PYTHON"

# ------------------------------------------------------------------- proxy
# A desktop launcher inherits none of the shell's environment, so a proxy set
# in ~/.bashrc is invisible to the app. Capture it once, here, where we still
# have the user's shell environment.
ENV_FILE="$HOME/.config/meeting-subtitles/env"
if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    {
        echo "# Environment for 会议字幕, read by meeting_subtitles/serve.py."
        echo "# A desktop launcher does not inherit your shell config, so any"
        echo "# proxy needed to reach huggingface.co has to be repeated here."
        echo "# Only used when a model still has to be downloaded; once the"
        echo "# models are cached the engine starts fully offline."
        for var in http_proxy https_proxy all_proxy no_proxy; do
            value="${!var:-}"
            # httpx rejects the bare "socks://" scheme GNOME emits.
            case "$value" in socks://*) value="socks5://${value#socks://}" ;; esac
            if [ -n "$value" ]; then
                echo "export $var=\"$value\""
            else
                echo "# export $var=\"http://127.0.0.1:7897\""
            fi
        done
    } > "$ENV_FILE"
    step "记录代理设置"
    note "$ENV_FILE"
fi

# ---------------------------------------------------------------- desktop
if [ "$WANT_DESKTOP" = "1" ]; then
    step "安装桌面入口"
    mkdir -p "$APPS" "$ICONS"
    cp "$REPO/meeting_subtitles/assets/meeting-subtitles.svg" "$ICON"
    cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=会议字幕
Name[en]=Meeting Subtitles
GenericName=实时中英对照字幕
GenericName[en]=Live bilingual subtitles
Comment=为 Zoom / Teams / 浏览器视频生成实时中英对照字幕并记录转录
Comment[en]=Live English-Chinese subtitles for any meeting, recorded to a transcript
Exec=$VENV/bin/meeting-subtitles
Path=$REPO
Icon=meeting-subtitles
Terminal=false
Categories=AudioVideo;Audio;
Keywords=subtitle;transcription;meeting;zoom;字幕;转录;会议;
StartupNotify=true
StartupWMClass=Meeting-subtitles
EOF
    chmod +x "$DESKTOP"
    update-desktop-database "$APPS" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    note "在应用列表里搜「会议字幕」即可启动"
fi

# ------------------------------------------------------------------ doctor
if [ "$DOWNLOAD_MODELS" = "1" ]; then
    step "下载默认模型"
    "$UV" run --no-sync python tools/models.py download
fi

step "环境自检"
"$VENV/bin/meeting-subtitles-doctor"

cat <<'EOF'

安装完成。

  启动          在应用列表里搜「会议字幕」，或运行 uv run --locked meeting-subtitles
  下载模型      uv run --locked python tools/models.py download
                下载全部默认模型后可离线启动；也可安装时加 --download-models
  恢复模型      uv run --locked python tools/models.py restore --from <备份目录>
  重新自检      uv run --locked meeting-subtitles-doctor

EOF
