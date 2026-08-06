#!/usr/bin/env bash
# One-shot setup for ig2claude. Safe to re-run.
set -e

echo "Installing Python dependencies..."
# instaloader is the primary Instagram profile-sweep backend for vid2learn
# (gallery-dl's profile extractor is routinely broken by IG; kept as a fallback).
pip3 install --upgrade yt-dlp gallery-dl instaloader faster-whisper pytesseract Pillow

# pip drops console scripts (yt-dlp) in the user base bin, which is often not on
# PATH. Put it on PATH for this run, and persist it to the shell profile so the
# yt-dlp launcher is found by later `python3 ig2claude.py` invocations.
USER_BIN="$(python3 -m site --user-base)/bin"
if [ -d "$USER_BIN" ] && ! command -v yt-dlp >/dev/null; then
  export PATH="$USER_BIN:$PATH"
  PROFILE="$HOME/.zshrc"
  LINE="export PATH=\"$USER_BIN:\$PATH\""
  if ! grep -qsF "$USER_BIN" "$PROFILE"; then
    echo "$LINE" >> "$PROFILE"
    echo "Added $USER_BIN to PATH in $PROFILE (restart your shell to pick it up)."
  fi
fi

echo
echo "Checking system binaries (ffmpeg + tesseract)..."
need_ffmpeg=$(command -v ffmpeg >/dev/null || echo yes)
need_tess=$(command -v tesseract >/dev/null || echo yes)

if [ -n "$need_ffmpeg$need_tess" ]; then
  echo "Some system tools are missing. Install them:"
  if [ "$(uname)" = "Darwin" ]; then
    [ -n "$need_ffmpeg" ] && echo "  brew install ffmpeg"
    if [ -n "$need_tess" ]; then
      if command -v brew >/dev/null; then
        echo "Installing tesseract via brew..."
        brew install tesseract || echo "  brew install tesseract failed; install it manually."
      else
        echo "  brew install tesseract   (Homebrew not found; install Homebrew first)"
      fi
    fi
  else
    [ -n "$need_ffmpeg" ] && echo "  sudo apt install ffmpeg      # (or your distro's package manager)"
    [ -n "$need_tess" ]   && echo "  sudo apt install tesseract-ocr"
  fi
else
  echo "ffmpeg and tesseract both present."
fi

echo
echo "Running preflight..."
python3 ig2claude.py --doctor

echo
echo "Setup done. Next: copy INSTAGRAM.md into ~/.claude/ and add its import line"
echo "to ~/.claude/CLAUDE.md (see README.md)."
