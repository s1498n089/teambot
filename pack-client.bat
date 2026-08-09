@echo off
rem ---------------------------------------------------------------
rem  Build the client bundle.   Usage:  pack-client.bat  (double-click)
rem
rem  Produces dist\a2a-client-YYYYMMDD.zip -- hand that file to anyone
rem  who wants to join the chatroom with their own agent. They unzip,
rem  edit one line in client.env (the hub address), and run bell.py.
rem
rem  The file list lives in make_client_zip.py, not here: a list in a
rem  script is checked by tests, a list in a batch file is not.
rem
rem  ASCII-only on purpose: cmd.exe parses .bat with the system
rem  codepage and mangles non-ASCII text.
rem
rem  The pause keeps the window open so you can read the output
rem  (it prints where the zip went) when double-clicked.
rem ---------------------------------------------------------------
cd /d "%~dp0"
uv run make_client_zip.py
pause
