#!/bin/sh
# minuta installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/damianpeaf/minuta/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --non-interactive --name Ana    # arguments go to `minuta setup`
#
# Environment:
#   MINUTA_REPO_URL  git URL or local folder to install from (default: the GitHub repository)
#   MINUTA_REF       branch or tag (default: the repository's default branch); `minuta update` keeps it
#   MINUTA_APP_DIR   where the source checkout lives (default: the platform's data folder + /app)
#   MINUTA_EXTRAS    package extras (default: "local", plus "cuda" when an NVIDIA GPU is present; "" for none)
#   MINUTA_PYTHON    Python version for the tool environment (default: 3.12)
#   MINUTA_NO_SETUP  set to 1 to install without running `minuta setup`
#
# Everything runs inside main(), called on the last line, so a download cut short runs nothing.
set -eu

say() { printf '%s\n' "minuta: $*"; }
fail() { printf '%s\n' "minuta: $*" >&2; exit 1; }

# Remove the app folder only if it is empty or this installer made it (it leaves a marker file).
clear_app_dir() {
  if [ -e "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ] && [ ! -e "$APP_DIR/.minuta-installer" ]; then
    fail "$APP_DIR exists and was not created by this installer; move it away or set MINUTA_APP_DIR"
  fi
  rm -rf "$APP_DIR"
}

main() {
  REPO_URL="${MINUTA_REPO_URL:-https://github.com/damianpeaf/minuta}"
  REF="${MINUTA_REF:-}"
  PYTHON_VERSION="${MINUTA_PYTHON:-3.12}"

  case "$(uname -s)" in
    Darwin) DEFAULT_APP_DIR="$HOME/Library/Application Support/minuta/app" ;;
    Linux) DEFAULT_APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/minuta/app" ;;
    *) fail "unsupported system $(uname -s); on Windows use install.ps1" ;;
  esac
  APP_DIR="${MINUTA_APP_DIR:-$DEFAULT_APP_DIR}"

  if [ "${MINUTA_EXTRAS+set}" = set ]; then
    EXTRAS="$MINUTA_EXTRAS"
  elif [ "$(uname -s)" = Linux ] && command -v nvidia-smi >/dev/null 2>&1; then
    EXTRAS="local,cuda"
  else
    EXTRAS="local"
  fi

  if ! command -v ffmpeg >/dev/null 2>&1; then
    say "ffmpeg is not installed. minuta needs it to save audio:"
    say "  macOS: brew install ffmpeg    Debian/Ubuntu: sudo apt install ffmpeg    Fedora: sudo dnf install ffmpeg"
  fi

  # 1. uv
  if ! command -v uv >/dev/null 2>&1; then
    if [ -x "$HOME/.local/bin/uv" ]; then
      PATH="$HOME/.local/bin:$PATH"
    else
      say "installing uv"
      command -v curl >/dev/null 2>&1 || fail "curl is needed to install uv"
      curl -LsSf https://astral.sh/uv/install.sh | sh
      PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    fi
    export PATH
  fi
  command -v uv >/dev/null 2>&1 || fail "uv was not found after installing it"

  # 2. source
  mkdir -p "$(dirname "$APP_DIR")"
  if command -v git >/dev/null 2>&1; then
    if [ -d "$APP_DIR/.git" ] && [ -e "$APP_DIR/.minuta-installer" ]; then
      say "updating $APP_DIR"
      git -C "$APP_DIR" remote set-url origin "$REPO_URL"
      git -C "$APP_DIR" fetch --depth 1 origin "${REF:-HEAD}"
      git -C "$APP_DIR" reset --hard FETCH_HEAD
    else
      say "downloading into $APP_DIR"
      clear_app_dir
      git clone --depth 1 ${REF:+--branch "$REF"} "$REPO_URL" "$APP_DIR"
    fi
  else
    say "git not found; downloading a tarball into $APP_DIR"
    clear_app_dir
    mkdir -p "$APP_DIR"
    if [ -d "$REPO_URL" ]; then
      (cd "$REPO_URL" && tar --exclude=.git --exclude=.venv -cf - .) | (cd "$APP_DIR" && tar -xf -)
    else
      # archive/<ref> takes a branch or a tag.
      curl -fsSL "${REPO_URL%/}/archive/${REF:-main}.tar.gz" | tar -xz --strip-components=1 -C "$APP_DIR"
    fi
  fi
  touch "$APP_DIR/.minuta-installer"

  # 3. the tool, with the versions pinned in uv.lock (exported to constraints.txt)
  # From inside the checkout with relative paths: uv splits a --constraints path at spaces
  # ("Application Support" on macOS).
  SPEC="."
  [ -n "$EXTRAS" ] && SPEC=".[${EXTRAS}]"
  say "installing the minuta command (extras: ${EXTRAS:-none})"
  if [ -f "$APP_DIR/constraints.txt" ]; then
    (cd "$APP_DIR" && uv tool install --force --reinstall --python "$PYTHON_VERSION" --constraints constraints.txt "$SPEC")
  else  # a release from before constraints.txt
    (cd "$APP_DIR" && uv tool install --force --reinstall --python "$PYTHON_VERSION" "$SPEC")
  fi
  BIN_DIR="$(uv tool dir --bin)"
  MINUTA="$BIN_DIR/minuta"
  [ -x "$MINUTA" ] || fail "minuta was not installed in $BIN_DIR"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "add $BIN_DIR to your PATH (or run: uv tool update-shell)" ;;
  esac

  # 4. setup
  if [ "${MINUTA_NO_SETUP:-}" = 1 ]; then
    say "installed. Run: $MINUTA setup"
    exit 0
  fi
  interactive=1
  for arg in "$@"; do [ "$arg" = "--non-interactive" ] && interactive=0; done
  if [ "$interactive" = 1 ] && [ ! -t 0 ] && [ -r /dev/tty ]; then
    # Piped from curl: questions are read from the terminal.
    exec "$MINUTA" setup --app-dir "$APP_DIR" "--install-extras=$EXTRAS" "--install-ref=$REF" "$@" </dev/tty
  fi
  exec "$MINUTA" setup --app-dir "$APP_DIR" "--install-extras=$EXTRAS" "--install-ref=$REF" "$@"
}

main "$@"
