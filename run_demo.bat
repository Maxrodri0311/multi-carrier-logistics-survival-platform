@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title "Skydropx - Frenet: Logistics Survival Analytics Platform"

echo ======================================================================
echo  Skydropx - Frenet: Logistics Survival Analytics Platform
echo  Automated Pipeline, In-Memory OLAP and Test Suite (1-Click Run)
echo ======================================================================
echo.

rem Resolucion de interprete Python (prioriza py -3 sobre python)
set "PY_CMD=py -3"
%PY_CMD% --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set "PY_CMD=python"
    %PY_CMD% --version >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] No se encontro un interprete de Python 3 en el sistema.
        exit /b 1
    )
)

echo [1/4] Generando dataset logistico estocastico (50,000 registros)...
%PY_CMD% src\data_generator.py --records 50000 --output data\raw_dataset.parquet
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Fallo en el generador de datos.
    exit /b %ERRORLEVEL%
)

echo.
echo [2/4] Ejecutando Motor Analitico Core (Kaplan-Meier, Cox HR y Semantic Layer)...
%PY_CMD% src\core_engine.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Fallo en la ejecucion del motor de supervivencia.
    exit /b %ERRORLEVEL%
)

echo.
echo [3/4] Ejecutando Suite Automatizada de Pruebas Unitarias (Pytest)...
%PY_CMD% -m pytest tests\test_suite.py -v
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Pruebas unitarias fallidas.
    exit /b %ERRORLEVEL%
)

echo.
echo [4/4] Ejecutando Benchmarks Cuantitativos de Latencia y Memoria (30 iteraciones)...
%PY_CMD% tests\benchmark.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Fallo en el benchmark de latencia.
    exit /b %ERRORLEVEL%
)

echo ======================================================================
echo  [OK] Ejecucion Exitosa: Pipeline, Pruebas y Benchmarks Completados al 100%%
echo ======================================================================
endlocal
