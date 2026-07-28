@echo off
rem ---------------------------------------------------------------
rem  A2A hub launcher.   Usage:  hub.bat   (or just double-click)
rem
rem  Settings go in server.env (see server.env.example) --
rem  bind address, port, public URL, auth. No need to edit this file.
rem
rem  ASCII-only on purpose: cmd.exe parses .bat with the system
rem  codepage and mangles non-ASCII text.
rem
rem  The pause at the end keeps the window open after the server
rem  stops, so you can still read the last error when double-clicked.
rem ---------------------------------------------------------------
cd /d "%~dp0"
uv run server.py
pause
