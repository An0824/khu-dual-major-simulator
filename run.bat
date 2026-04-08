@echo off
chcp 65001 > nul
title KHU 다전공 시뮬레이터 실행기

echo ==========================================
echo   🎓 KHU 다전공 시뮬레이터 실행기
echo ==========================================
echo.

:: 현재 폴더로 이동
cd /d "%~dp0"

:: 파이썬 설치 여부 확인
python --version >nul 2>&1
if errorlevel 1 goto NOPYTHON

:: 가상환경 폴더가 없으면 생성 단계로 이동
if not exist venv\Scripts\activate goto MAKEVENV
goto RUNAPP

:MAKEVENV
echo [안내] 최초 실행입니다. 초기 환경을 설정합니다. (1~2분 소요)
python -m venv venv
goto RUNAPP

:NOPYTHON
echo [에러] PC에 Python이 설치되어 있지 않거나 경로(PATH) 설정이 안 되어 있습니다.
echo 파이썬 홈페이지에서 설치 후 다시 실행해주세요.
pause
exit /b

:RUNAPP
:: 가상환경 켜기 및 패키지 설치
call venv\Scripts\activate
echo [안내] 필수 라이브러리를 설치 중입니다...
pip install -r requirements.txt -q

:: 앱 실행
echo.
echo [안내] 플래너를 실행합니다. 잠시 후 웹 브라우저가 자동으로 열립니다!
streamlit run app.py

pause