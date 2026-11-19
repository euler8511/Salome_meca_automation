import os
import subprocess
import sys
import traceback

log_path = "C:/Users/ghchoi/Desktop/Antigravity/Salome_meca_automation/aster_execution_log.txt"

def log(msg):
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(str(msg) + "\n")

try:
    current_dir = "C:/Users/ghchoi/Desktop/Antigravity/Salome_meca_automation"
    os.chdir(current_dir)

    export_file_path = "C:/Users/ghchoi/Desktop/Antigravity/Salome_meca_automation/run_case.export"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("=== EXECUTION LOG ===\n")

    log("[Salome Batch] Code_Aster 해석 엔진(as_run) 구동 준비...")
    log(f"대상 프로파일: {export_file_path}")

    cmd = f'as_run "{export_file_path}"'
    log(f"실행 명령: {cmd}")

    res = subprocess.run(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding='utf-8', errors='replace'
    )

    log("=== as_run STDOUT/STDERR ===")
    log(res.stdout)
    log(f"=== returncode: {res.returncode} ===")

    sys.exit(res.returncode)

except Exception:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n=== UNCAUGHT EXCEPTION ===\n")
        f.write(traceback.format_exc())
    print("=== UNCAUGHT EXCEPTION ===")
    traceback.print_exc()
    sys.exit(1)
