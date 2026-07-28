@echo off
rem ---------------------------------------------------------------
rem  A2A bell launcher.
rem
rem      bell.bat <name> <command...>
rem
rem      bell.bat alice claude -r
rem      bell.bat bob   claude --dangerously-skip-permissions
rem      bell.bat carol codex
rem      bell.bat alice                  uses the default command
rem
rem  Everything after the name is passed through UNTOUCHED. It is the
rem  command you would have typed anyway -- you just put "bell.bat
rem  <name>" in front of it. Nothing is translated here.
rem
rem  Why nothing is translated:
rem    This file used to accept two keywords of our own invention,
rem    "allow" and "yolo", and map them to Claude permission flags.
rem    Anything it did not recognise was ignored -- which meant
rem    "bell.bat alice yolooo" started in NORMAL mode, silently, and
rem    you believed you had turned checks off. A typo was swallowed.
rem
rem    Passing the command straight through fixes that by removing
rem    the middle layer: a mistyped flag is now reported by the tool
rem    that owns it. We also cannot keep a list of another product's
rem    flags up to date, so any such list is stale by design.
rem
rem  Permission flags are yours to choose. Anthropic recommends
rem  --dangerously-skip-permissions only for sandboxes with no
rem  internet access; agents here fetch external pages and forward
rem  them into the room. See Claude's own docs for what each flag
rem  does -- we deliberately do not restate it, because a copy here
rem  would go out of date without anyone noticing.
rem
rem  Keep this file ASCII-only. cmd.exe parses .bat with the system
rem  codepage, so non-ASCII comments get mis-split into commands.
rem  All checks and messages live in bell.py, which handles UTF-8.
rem
rem  %~dp0 = folder of this .bat, so shortcuts work from anywhere.
rem  It must be used before any shift, which renumbers %0 too.
rem ---------------------------------------------------------------
cd /d "%~dp0"

if "%~1"=="" (
    echo.
    echo   Missing agent name.
    echo.
    echo     bell.bat alice claude -r
    echo     bell.bat bob   codex
    echo     bell.bat alice              ^(uses the default command^)
    echo.
    echo   The name maps to state\cursor-^<name^>.txt
    echo.
    pause
    exit /b 1
)

set "NAME=%~1"
shift

rem Collect every remaining argument into CMD.
rem
rem   Why not simply %*: it always expands to ALL arguments including
rem   the name, and shift does not change it. cmd.exe has no built-in
rem   "the rest of the arguments", so this loop is the standard way.
rem   Do not "simplify" it back to %* -- the name would be passed to
rem   the agent CLI as an extra argument.
rem
rem   %1 and not %~1 on purpose: %~1 strips quotes, so an argument
rem   containing spaces would arrive as two arguments. Keeping the
rem   user's own quotes lets it pass through as one.
set "CMD="
:collect
if "%~1"=="" goto ready
set "CMD=%CMD% %1"
shift
goto collect

:ready
rem A default is fine. An INVISIBLE default is not -- so it is echoed.
rem Seeing this line is also how a first-time user learns that a
rem command can be typed here at all.
if not defined CMD (
    set "CMD=claude --resume"
    echo   [bell] no command given, using: claude --resume
)

rem The space before %CMD% is required. The loop above leaves a leading
rem space in CMD, but the default branch does not -- without the space
rem here, "-- claude --resume" would come out as "--claude --resume".
uv run bell.py --name "%NAME%" -- %CMD%
