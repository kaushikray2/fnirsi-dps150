@echo off
cd /d %~dp0
python -c "import PySide6, serial" 2>nul || pip install -r requirements.txt
python fnirsi_gui.py %*
