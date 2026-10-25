"""python -m terminal_invaders.audio runs the manual check, as python -m terminal_invaders.audio.check does."""

import sys  # pragma: no cover

from .check import main  # pragma: no cover

sys.exit(main())  # pragma: no cover
