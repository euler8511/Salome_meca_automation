import os
import subprocess
import sys

# 실행 기준점을 현재 폴더로 강제 고정
current_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(current_dir)

print("[Salome Internal] Code_Aster 해석 엔진(as_run)을 호출합니다...")

# Salome 환경 내부에서 as_run 실행
result = subprocess.run(
    "as_run run_case.export", 
    shell=True, capture_output=True, text=True
)

# 백그라운드에서 진행된 STDOUT, STDERR 로그를 파일로 기록
with open("aster_execution_log.txt", "w", encoding="utf-8") as f:
    f.write("=== STDOUT ===\n" + str(result.stdout))
    if result.stderr:
        f.write("\n=== STDERR ===\n" + str(result.stderr))

sys.exit(result.returncode)
