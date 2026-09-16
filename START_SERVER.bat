@echo off
echo.
echo ============================================
echo   AYPEE INDUSTRIES - Quotation System
echo ============================================
echo.
echo Starting server...
echo.
echo   Open your browser and go to:
echo   http://localhost:5000
echo.
echo   Login:  admin / admin123
echo.
echo   Press CTRL+C to stop the server
echo ============================================
echo.
python server.py
if %errorlevel% neq 0 (
    echo.
    echo Python not found! Please install Python from:
    echo https://www.python.org/downloads/
    echo.
    echo Make sure to check "Add Python to PATH" during install
    echo.
    pause
)
