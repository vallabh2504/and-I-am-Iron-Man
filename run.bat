@echo off
REM Launch the snap listener minimised. Close the window to stop it.
start "Snap-To-Dictate" /min cmd /c "python "%~dp0snap_to_dictate.py" & pause"
