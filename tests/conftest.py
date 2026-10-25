"""Every test runs without sound unless it starts the audio helper itself with its own INVADERS_AUDIO."""

import os

os.environ["INVADERS_AUDIO"] = "none"
