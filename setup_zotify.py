#!/usr/bin/env python3
"""
One-time interactive Spotify login for zotify.

Run this ONCE, by hand, in a terminal — never from the web server:

    .venv-dl/bin/python setup_zotify.py

It opens a Spotify OAuth login in your browser and saves a refresh token so
every later download is fully non-interactive.

Why this wrapper instead of calling `zotify` directly:
zotify hardcodes the OAuth callback server to listen on ALL network interfaces
(0.0.0.0), which makes macOS pop the "find devices on your local network"
prompt. The callback is only ever hit by your own browser over loopback, so we
patch librespot to bind 127.0.0.1 only — the prompt never appears, and nothing
is exposed to the LAN.

Recommended: log in with a BURNER Spotify account, not your main one. A free
account is enough — downloads are 96 kbps, which is all the analysis needs, and
it keeps any ban risk off your primary account.
"""
import sys
from pathlib import Path

# Must run inside the zotify venv (.venv-dl), not the app venv.
try:
    import zotify  # noqa: F401
    from librespot.oauth import OAuth
except ImportError:
    sys.exit(
        "This must run inside the zotify venv:\n"
        "    .venv-dl/bin/python setup_zotify.py\n"
        "If .venv-dl is missing, create it:\n"
        "    /opt/homebrew/bin/python3.12 -m venv .venv-dl\n"
        "    .venv-dl/bin/pip install git+https://github.com/Googolplexed0/zotify.git"
    )

# Force the OAuth callback server to bind loopback only (no macOS prompt).
_orig_set_listen_all = OAuth.set_listen_all
OAuth.set_listen_all = lambda self, listen_all=True: _orig_set_listen_all(self, False)

# zotify's login retry loop (boot()) reruns the whole OAuth flow when a login
# attempt fails — e.g. a transient ECONNREFUSED from Spotify's access point —
# but it never releases the callback server socket from the previous attempt,
# so the retry dies with "Address already in use" on port 4381. Two guards:
#   1. allow_reuse_address so a socket in TIME_WAIT doesn't block a rebind.
#   2. close the callback server after every flow() so the port is free for the
#      next attempt.
OAuth.CallbackServer.allow_reuse_address = True

_orig_flow = OAuth.flow
def _flow_then_release_port(self):
    try:
        return _orig_flow(self)
    finally:
        server = getattr(self, "_OAuth__server", None)
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
OAuth.flow = _flow_then_release_port

AUDIO_DIR = Path(__file__).parent / ".audio_files"
AUDIO_DIR.mkdir(exist_ok=True)

# A tiny, free, well-known track to confirm login + download work end to end.
# (Rick Astley – Never Gonna Give You Up.)
TEST_TRACK = "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8"

if __name__ == "__main__":
    print("Starting zotify login (browser window will open)…")
    print("Tip: use a burner Spotify account, not your main one.\n")
    sys.argv = [
        "zotify",
        "--download-quality", "normal",
        "--codec", "copy",
        "--root-path", str(AUDIO_DIR),
        "--output-single", "{id}",
        "--lyrics-to-file", "False",
        "--lyrics-to-metadata", "False",
        "--md-save-lyrics", "False",
        "--album-art-jpg-file", "False",
        "--md-save-genres", "False",
        "--md-disc-track-totals", "False",
        "--disable-song-archive", "True",
        "--disable-directory-archives", "True",
        "--retry-attempts", "4",   # transient AP refusals self-heal now
        TEST_TRACK,
    ]
    from zotify.__main__ import main
    main()
    print("\nLogin complete. Credentials saved — the web server can now "
          "download non-interactively (USE_LOCAL_ANALYSIS=true).")
