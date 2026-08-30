#!/bin/bash
# airplaymatrix-quit.sh — bound to Shift+X in labwc (~/.config/labwc/rc.xml)
# to let the AirPlay Desk Display app be closed on demand.
#
# Sends SIGTERM rather than SIGKILL: both main.py's (app/, Pi 5/Qt6) and
# app_qt5/main.py's (Pi Zero WH/Qt5) install a handler that turns SIGTERM
# into a normal app.quit(), so aboutToQuit still fires and
# AppController.shutdown() gets to clean up (stop the receiver supervisor,
# clear the matrix) instead of leaving things running. A given device only
# ever runs one of the two builds, and "python3 -m app" as a pattern matches
# both "python3 -m app.main" and "python3 -m app_qt5.main" -- no need to
# know which one is installed here.
pkill -TERM -f "python3 -m app"
