@echo off
cd /d "C:\Users\lucia\Downloads\files"
set PYTHONUTF8=1
"C:\Users\lucia\AppData\Local\Programs\Python\Python312\python.exe" monitor_olx.py >> monitor_log.txt 2>&1
