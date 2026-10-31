@echo off
rem Builds dist\bk2318-printer.exe (run from the project folder, venv activated or not)
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed --name bk2318-printer --add-data "templates;templates" b2318.py || exit /b 1
echo.
echo Done: dist\bk2318-printer.exe
