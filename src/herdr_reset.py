#!/usr/bin/env python3
"""herdr-reset: close every other pane on the current herdr tab, keeping the caller's terminal."""

import sys

import herdr_ai

if __name__ == "__main__":
    from platform_runtime import configure_stdio
    configure_stdio()
    sys.exit(herdr_ai.reset_main(sys.argv[1:]))
