#!/bin/bash
# airplaymatrix-quit.sh — bound to Shift+X in labwc (~/.config/labwc/rc.xml)
# to let the AirPlay Desk Display app be closed on demand.
#
# Sends SIGTERM rather than SIGKILL: main.py installs a handler that turns
# SIGTERM into a normal app.quit(), so aboutToQuit still fires and
# AppController.shutdown() gets to clean up (stop the receiver supervisor,
# clear the matrix) instead of leaving things running.
pkill -TERM -f "python3 -m app.main"
