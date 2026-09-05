#!/usr/bin/env bash
# Install 会议字幕 / Meeting Subtitles for the current user.
#
#   ./install.sh                 # virtualenv + dependencies + desktop entry
#   ./install.sh --cuda cu128    # build of PyTorch to install (default cu129)
#   ./install.sh --cpu           # no CUDA; runs, but not in real time
#   ./install.sh --pypi-engine   # use the released whisperlivekit, not the pin
#   ./install.sh --no-desktop    # skip the GNOME application entry
#   ./install.sh --remove        # remove the desktop entry (keeps .venv)
#
# Everything is per-user: the virtualenv lives in this directory and the
# desktop entry in ~/.local/share. The only thing that needs sudo is apt, and
# this script never calls it for you -- it prints the command and stops.

set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"

VENV="$REPO/.venv"
PYTHON="$VENV/bin/python"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
DESKTOP="$APPS/meeting-subtitles.desktop"
ICON="$ICONS/meeting-subtitles.svg"

# whisperlivekit gained per-session decoder context in this commit. Without it
# the built-in glossary is silently ignored, and acronyms like "LoRA", "vLLM"
# and "NeurIPS" come out as whatever they sound like. The released 0.2.26 does
# not have it yet, so pin the commit until upstream cuts 0.2.27.
ENGINE_PIN="whisperlivekit[translation] @ git+https://github.com/QuentinFuxa/WhisperLiveKit@b781ce9"

# CTranslate2 -- the Whisper encoder -- links against libcublas.so.12, i.e.
# CUDA 12. Plain `pip install torch` now resolves to a build carrying CUDA 13
# instead, and the engine then fails on every audio chunk with "Library
# libcublas.so.12 is not found", while still starting up and answering health
# checks. So install PyTorch from a CUDA 12 index explicitly, before anything
# can pull the default one in.
CUDA="cu129"
USE_PIN=1
WANT_DESKTOP=1
for arg in "$@"; do
    case "$arg" in
        --cuda) NEXT_IS_CUDA=1 ;;
        --cuda=*) CUDA="${arg#--cuda=}" ;;
        --cpu) CUDA="cpu" ;;
        --pypi-engine) USE_PIN=0 ;;
        --no-desktop)  WANT_DESKTOP=0 ;;
        --remove)
            rm -f "$DESKTOP" "$ICON"
            update-desktop-database "$APPS" 2>/dev/null || true
            gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
            echo "已移除桌面入口。虚拟环境 $VENV 未删除。"
            exit 0 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        cu*|cpu) [ "${NEXT_IS_CUDA:-0}" = "1" ] && { CUDA="$arg"; NEXT_IS_CUDA=0; } \
                     || { echo "未知参数: $arg" >&2; exit 2; } ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[36m==\033[0m %s\n' "$1"; }
note() { printf '   \033[2m%s\033[0m\n' "$1"; }

# --------------------------------------------------------------- system deps
step "检查系统依赖"
MISSING=()
command -v ffmpeg  >/dev/null || MISSING+=(ffmpeg)
command -v pactl   >/dev/null || MISSING+=(pulseaudio-utils)
python3 -c "import tkinter" 2>/dev/null || MISSING+=(python3-tk)
python3 -c "import venv"    2>/dev/null || MISSING+=(python3-venv)
# Not `fc-list | grep -q`: grep exits at the first match, fc-list dies of
# SIGPIPE, and `set -o pipefail` then reports the whole pipeline as failed --
# so an installed font looks missing. Match against a variable instead.
FONTS="$(fc-list 2>/dev/null || true)"
grep -qi "noto sans cjk\|wenquanyi\|source han\|noto sans sc" <<<"$FONTS" \
    || MISSING+=(fonts-noto-cjk)

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "缺少以下系统包：${MISSING[*]}"
    echo
    echo "请先运行："
    echo "    sudo apt update && sudo apt install -y ${MISSING[*]}"
    echo
    echo "装好后重新运行 ./install.sh"
    exit 1
fi
note "ffmpeg / pactl / tkinter / venv / 中文字体 都在"

# ---------------------------------------------------------------- interpreter
step "检查 Python"
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info[:2] >= (3, 11) else 0)')
if [ "$PY_OK" != "1" ]; then
    echo "需要 Python 3.11 或更高，当前是 $(python3 -V)。" >&2
    exit 1
fi
note "$(python3 -V)"

# ------------------------------------------------------------------- venv
step "创建虚拟环境"
if [ -x "$PYTHON" ]; then
    note "已存在 $VENV"
else
    python3 -m venv "$VENV"
    note "已创建 $VENV"
fi
"$PYTHON" -m pip install --upgrade pip --quiet
note "pip 已更新"

# ------------------------------------------------------------------ pytorch
step "安装 PyTorch ($CUDA)"
if [ "$CUDA" = "cpu" ]; then
    WANT="none"
else
    WANT="${CUDA#cu}"; WANT="${WANT:0:2}"        # cu129 -> 12
fi
HAVE="$("$PYTHON" - <<'PY' 2>/dev/null || true
try:
    import torch
    print((torch.version.cuda or "none").split(".")[0])
except Exception:
    print("")
PY
)"
if [ "$HAVE" = "$WANT" ]; then
    note "已安装的 PyTorch 已经是 CUDA $HAVE，跳过"
else
    note "CTranslate2 需要 CUDA 12 的 libcublas；PyPI 默认的 torch 现在带 CUDA 13"
    [ -n "$HAVE" ] && note "当前是 CUDA $HAVE，强制重装"
    # --force-reinstall: pip treats an already-installed torch as satisfying
    # the requirement no matter which index it came from, so without this a
    # CUDA 13 build silently stays put.
    "$PYTHON" -m pip install --force-reinstall torch torchaudio \
        --index-url "https://download.pytorch.org/whl/$CUDA"
fi

# --------------------------------------------------------------- the package
step "安装其余依赖"
if [ "$USE_PIN" = "1" ]; then
    note "引擎使用固定提交（支持领域术语条件化）"
    "$PYTHON" -m pip install "$ENGINE_PIN"
else
    note "引擎使用 PyPI 发布版（领域术语表会被忽略）"
fi
"$PYTHON" -m pip install -e .

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
step "环境自检"
"$VENV/bin/meeting-subtitles-doctor" || true

cat <<'EOF'

安装完成。

  启动          在应用列表里搜「会议字幕」，或运行 .venv/bin/meeting-subtitles
  首次启动      引擎会下载约 18 GB 模型；已有备份可用
                .venv/bin/python tools/models.py restore --from <备份目录>
  重新自检      .venv/bin/meeting-subtitles-doctor

EOF
