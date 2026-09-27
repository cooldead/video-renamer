#!/bin/sh
# Install Video Renamer under a prefix. Used by the pacman package and the Flatpak.
#   packaging/install.sh <destdir> <prefix>     e.g. "$pkgdir" /usr, or "" /app
# The Python code goes into a private folder (not site-packages), so the
# package keeps working when the system Python is upgraded.
set -eu
destdir=$1
prefix=$2
src="$(dirname "$(readlink -f "$0")")/.."
id=io.github.cooldead.VideoRenamer
lib="$prefix/lib/video-renamer"

install -d "$destdir$lib/video_renamer"
install -m644 "$src"/video_renamer/*.py "$destdir$lib/video_renamer/"
python3 -m compileall -q -d "$lib/video_renamer" "$destdir$lib/video_renamer"

install -d "$destdir$prefix/bin"
cat > "$destdir$prefix/bin/video-renamer" <<EOF
#!/bin/sh
PYTHONPATH="$lib\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m video_renamer "\$@"
EOF
chmod 755 "$destdir$prefix/bin/video-renamer"

install -Dm644 "$src/data/$id.desktop" "$destdir$prefix/share/applications/$id.desktop"
install -Dm644 "$src/data/$id.svg" "$destdir$prefix/share/icons/hicolor/scalable/apps/$id.svg"
install -Dm644 "$src/data/$id.metainfo.xml" "$destdir$prefix/share/metainfo/$id.metainfo.xml"
install -Dm644 "$src/LICENSE" "$destdir$prefix/share/licenses/video-renamer/LICENSE"
