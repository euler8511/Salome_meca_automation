import os
import subprocess

current_dir = os.path.dirname(os.path.abspath(__file__))
salome_path = r"C:\Users\ghchoi\Desktop\v2023\run_mesa_salome.bat"

cmd_str = f'"{salome_path}"'

print("Salome를 실행합니다. (창을 닫을 때까지 파이썬 대기 중...)")

# run을 사용하면 GUI가 닫힐 때까지 대기 후 결과(로그) 캡처
result = subprocess.run(
    cmd_str, shell=True, capture_output=True, text=True, cwd=current_dir
)

print("Salome가 종료되었습니다.")