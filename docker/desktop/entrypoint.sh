#!/bin/sh
set -eu
umask 077
mkdir -p /config/.supervisor /config/.config /config/.cache /config/.local/share /config/Desktop /config/Downloads
exec /usr/bin/supervisord -c /deployment/supervisord.conf
