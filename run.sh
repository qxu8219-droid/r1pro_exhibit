#!/bin/sh
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
exec sh "$PACKAGE_DIR/exhibit/run.sh" "$@"
