#!/bin/sh
# install.sh [--completion bash|zsh|fish]
#
# Fresh install of this adbsync fork from a checkout.
#
#   ./install.sh                     install the adbsync package
#   ./install.sh --completion zsh    also install shell completion
#
# The completion script is written to the conventional per-user location.
# Your shell startup files (~/.zshrc, ~/.bashrc, ...) are NEVER edited
# automatically; the script prints the one line to add yourself.
set -eu

cd "$(dirname "$0")"

COMPLETION=""
if [ "${1:-}" = "--completion" ]; then
    COMPLETION="${2:-}"
    case "$COMPLETION" in
        bash|zsh|fish) ;;
        *) echo "usage: $0 [--completion bash|zsh|fish]" >&2; exit 2 ;;
    esac
elif [ $# -ne 0 ]; then
    echo "usage: $0 [--completion bash|zsh|fish]" >&2
    exit 2
fi

if command -v uv >/dev/null 2>&1; then
    uv tool install --force .
elif command -v pip3 >/dev/null 2>&1; then
    pip3 install --force-reinstall --no-deps .
elif command -v pip >/dev/null 2>&1; then
    pip install --force-reinstall --no-deps .
else
    echo "error: need uv or pip to install" >&2
    exit 1
fi
hash -r 2>/dev/null || true

if [ -z "$COMPLETION" ]; then
    echo "installed. rerun with '--completion bash|zsh|fish' to set up tab completion,"
    echo "or generate it anytime with: adbsync completion $COMPLETION"
    exit 0
fi

if ! command -v adbsync >/dev/null 2>&1; then
    echo "error: adbsync not on PATH after install" >&2
    exit 1
fi

case "$COMPLETION" in
    bash)
        DEST="$HOME/.local/share/bash-completion/completions/adbsync"
        mkdir -p "$(dirname "$DEST")"
        adbsync completion bash > "$DEST"
        echo "wrote $DEST"
        echo "then ensure bash-completion is loaded (most distros do this by default);"
        echo "otherwise add to ~/.bashrc:  source $DEST"
        ;;
    zsh)
        DEST="$HOME/.zsh/completions/_adbsync"
        mkdir -p "$(dirname "$DEST")"
        adbsync completion zsh > "$DEST"
        echo "wrote $DEST"
        echo "then add BEFORE 'compinit' in ~/.zshrc:  fpath=(~/.zsh/completions \$fpath)"
        ;;
    fish)
        DEST="$HOME/.config/fish/completions/adbsync.fish"
        mkdir -p "$(dirname "$DEST")"
        adbsync completion fish > "$DEST"
        echo "wrote $DEST (fish loads it automatically)"
        ;;
esac
