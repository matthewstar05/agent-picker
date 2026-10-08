#!/usr/bin/env python3
"""agent-route: restart every agent on the current herdr tab in the current directory."""

import sys

import herdr_ai

if __name__ == "__main__":
    sys.exit(herdr_ai.route_main(sys.argv[1:]))
