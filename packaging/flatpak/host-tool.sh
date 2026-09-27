#!/bin/sh
# Installed in the Flatpak as makemkvcon, mkvmerge, rsync, ffprobe and gio: runs
# the program of that name on the host, so the sandbox needs no disc drives
# and the user's MakeMKV key and settings are the ones MakeMKV itself uses.
# Falls back to the program's own Flatpak. Exit code 127 = not installed.
tool=$(basename "$0")

# The host program starts in this folder; /app and /usr here aren't the host's.
case $PWD in
    /app | /app/* | /usr | /usr/*) cd "$HOME" ;;
esac

if flatpak-spawn --host sh -c 'command -v "$1" >/dev/null' sh "$tool"; then
    exec flatpak-spawn --host --watch-bus "$tool" "$@"
fi

case $tool in
    makemkvcon) app=com.makemkv.MakeMKV ;;
    mkvmerge) app=org.bunkus.mkvtoolnix-gui ;;
    *) app= ;;
esac
if [ -n "$app" ] && flatpak-spawn --host flatpak info "$app" >/dev/null 2>&1; then
    exec flatpak-spawn --host --watch-bus flatpak run --command="$tool" "$app" "$@"
fi

echo "$tool is not installed on this computer" >&2
exit 127
