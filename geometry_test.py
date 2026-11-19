import os
import subprocess

current_dir = os.path.dirname(os.path.abspath(__file__))
script_path = os.path.join(current_dir, "generate_geometry.py")


# 2. Salome 실행 파일 경로 설정
salome_path = r"C:\Users\ghchoi\Desktop\v2023\run_mesa_salome.bat"

cmd_str = f'"{salome_path}" -t -w1 "{script_path}"'



print("=" * 60)
print(f" [Controller] 작업 폴더: {current_dir}")
print(" [Controller] Salome 백그라운드 자동화를 시작합니다...")
print("=" * 60)

try:
    result = subprocess.run(
        cmd_str, shell=True, capture_output=True, text=True, cwd=current_dir
    )

    print("\n[SUCCESS] Salome 프로세스가 완료되었습니다. 폴더를 확인하세요.")

except subprocess.CalledProcessError as e:
    print(f"\n[ERROR] Salome 실행 중 오류 발생 (종료 코드: {e.returncode})")
    
    