"""Terminal Invaders: a deterministic arcade game with no runtime dependencies."""

import sys

__version__ = "2.0.0.dev0"

# Every launcher imports this first, so an older Python gets a sentence instead of a traceback from 3.11 syntax.
# Keep this module parseable by Python 3.6: stock macOS python3 (Xcode Command Line Tools) is 3.9.
if sys.version_info < (3, 11):
    sys.exit(
        "Terminal Invaders needs Python 3.11 or newer, but this is Python %s.\n"
        "Install a newer one (Homebrew: brew install python@3.13), then run it with that, for example\n"
        "  python3.13 invaders.py\n"
        "or run ./setup.sh, which installs the game with the Python you name." % sys.version.split()[0]
    )
