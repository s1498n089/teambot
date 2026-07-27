@echo off
rem ---------------------------------------------------------------
rem  A2A bell launcher.
rem
rem    bell.bat alice            normal - Claude asks before acting
rem    bell.bat alice allow      --allow-dangerously-skip-permissions
rem    bell.bat alice yolo       --dangerously-skip-permissions
rem
rem  Difference between the two flags:
rem    allow = makes skipping AVAILABLE; Claude still asks by default,
rem            you can switch to skip-mode during the session.
rem    yolo  = skipping is ON from the start; nothing is ever asked.
rem
rem  Anthropic recommends these only for sandboxes with no internet
rem  access. Agents here fetch external pages and forward them into
rem  the room, so treat yolo as "I know what this task will touch".
rem
rem  Keep this file ASCII-only. cmd.exe parses .bat with the system
rem  codepage, so non-ASCII comments get mis-split into commands.
rem  All checks and messages live in bell.py, which handles UTF-8.
rem
rem  %~dp0 = folder of this .bat, so shortcuts work from anywhere.
rem  --resume (not -c): see doc\TUTORIAL.md chapter 8.
rem ---------------------------------------------------------------
cd /d "%~dp0"

if "%~1"=="" (
    echo.
    echo   Missing agent name.
    echo.
    echo     bell.bat alice
    echo     bell.bat bob
    echo     bell.bat alice allow    ^(skipping becomes available^)
    echo     bell.bat alice yolo     ^(skipping is on from the start^)
    echo.
    echo   The name maps to state\cursor-^<name^>.txt
    echo.
    pause
    exit /b 1
)

rem Second argument is optional. Anything unrecognised is ignored,
rem so a typo can never silently turn permission checks off.
set EXTRA=
if /i "%~2"=="allow" (
    set EXTRA=--allow-dangerously-skip-permissions
    echo   [allow] permission skipping is AVAILABLE this session
)
if /i "%~2"=="yolo" (
    set EXTRA=--dangerously-skip-permissions
    echo   [YOLO] ALL permission checks are OFF from the start
)

uv run bell.py --name %1 -- claude --resume %EXTRA%
