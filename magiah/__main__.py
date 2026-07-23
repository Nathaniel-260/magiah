# -*- coding: utf-8 -*-
import sys

from .cli import main

# propagate the exit code: a stage that fails its prerequisite check returns 1,
# and the UI's scan runner relies on that non-zero code to stop the stage chain
sys.exit(main())
