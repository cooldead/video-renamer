#!/bin/sh
# Launch Video Renamer from its project folder. Optional argument: a folder or video file to open.
here="$(dirname "$(readlink -f "$0")")"
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m video_renamer "$@"
