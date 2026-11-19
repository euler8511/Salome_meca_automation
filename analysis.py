import os
import subprocess
import sys
import ctypes
import time

# ==============================================================================
# 0. 사용자 설정 (환경변수 우선, 없으면 기본값 사용)
# ==============================================================================
SALOME_BAT = os.environ.get(
    "SALOME_BAT",
    r"C:\Users\ghchoi\Desktop\v2023\run_salome.bat",
)
TIMEOUT_SEC = int(os.environ.get("ASTER_TIMEOUT_SEC", "3600"))  # 프로세스 레벨 타임아웃

# ==============================================================================
# 1. 파일 및 경로 설정 (Windows 표준 절대 경로 적용)
# ==============================================================================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

MED_FILE = os.path.normpath(os.path.join(CURRENT_DIR, "mesh.med"))
COMM_FILE = os.path.normpath(os.path.join(CURRENT_DIR, "analysis.comm"))
EXPORT_FILE = os.path.normpath(os.path.join(CURRENT_DIR, "run_case.export"))
MESS_FILE = os.path.normpath(os.path.join(CURRENT_DIR, "analysis.mess"))
RMED_FILE = os.path.normpath(os.path.join(CURRENT_DIR, "result.rmed"))
RESU_FILE = os.path.normpath(os.path.join(CURRENT_DIR, "analysis.resu"))
RUNNER_SCRIPT = os.path.normpath(os.path.join(CURRENT_DIR, "run_aster_batch.py"))
LOG_FILE = os.path.normpath(os.path.join(CURRENT_DIR, "aster_execution_log.txt"))

# ==============================================================================
# 2. 해석 파라미터
# ==============================================================================
YOUNGS_MODULUS = 200000000000.0  # 탄성계수 (Pa)
POISSON_RATIO = 0.3              # 푸아송 비
APPLIED_FORCE_Z = -50000000.0    # Z축 하중 (N)

ASTER_SUCCESS_MARKER = "arret normal"
ASTER_ERROR_MARKERS = ("<fatal alarm>", "<exception>", "erreur", "<f>", "arret sur erreur")


def show_notification(title: str, message: str, is_error: bool = False) -> None:
    """Windows 팝업 메시지 박스로 결과 알림 (항상 화면 최상단에 표시)"""
    icon = 0x10 if is_error else 0x40  # MB_ICONERROR(0x10) / MB_ICONINFORMATION(0x40)
    MB_TOPMOST = 0x40000
    ctypes.windll.user32.MessageBoxW(0, message, title, icon | MB_TOPMOST)


def check_prerequisites() -> None:
    if not os.path.exists(MED_FILE):
        print(f"\n[경고] 메쉬 파일이 없습니다: {MED_FILE}")
        sys.exit(1)
    if not os.path.exists(SALOME_BAT):
        print(f"\n[경고] Salome 실행 배치 파일을 찾을 수 없습니다: {SALOME_BAT}")
        sys.exit(1)


def generate_comm_file() -> None:
    comm_content = f"""DEBUT(LANG='EN')

mesh = LIRE_MAILLAGE(UNITE=20,
                     VERI_MAIL=_F(VERIF='OUI'))

model = AFFE_MODELE(AFFE=_F(MODELISATION='3D',
                            PHENOMENE='MECANIQUE',
                            TOUT='OUI'),
                    MAILLAGE=mesh)

mater = DEFI_MATERIAU(ELAS=_F(E={YOUNGS_MODULUS},
                              NU={POISSON_RATIO}))

fieldmat = AFFE_MATERIAU(AFFE=_F(MATER=mater,
                                 TOUT='OUI'),
                         MAILLAGE=mesh,
                         MODELE=model)

fix = AFFE_CHAR_MECA(DDL_IMPO=_F(DX=0.0,
                                 DY=0.0,
                                 DZ=0.0,
                                 GROUP_MA='fix'),
                     MODELE=model)

load0 = AFFE_CHAR_MECA(FORCE_FACE=_F(FZ={APPLIED_FORCE_Z},
                                     GROUP_MA='load_z'),
                       MODELE=model)

reslin = MECA_STATIQUE(CHAM_MATER=fieldmat,
                       EXCIT=(_F(CHARGE=fix),
                              _F(CHARGE=load0)),
                       MODELE=model)

unnamed = CALC_CHAMP(CRITERES=('SIEQ_ELGA', 'SIEQ_ELNO', 'SIEQ_NOEU'),
                     RESULTAT=reslin)

IMPR_RESU(RESU=(_F(NOM_CHAM='DEPL',
                   RESULTAT=reslin),
                _F(NOM_CHAM=('SIEQ_ELGA', 'SIEQ_ELNO', 'SIEQ_NOEU'),
                   RESULTAT=unnamed)),
          UNITE=80)

IMPR_RESU(FORMAT='RESULTAT',
          RESU=_F(NOM_CHAM='DEPL',
                  RESULTAT=reslin),
          UNITE=8)

FIN()
"""
    with open(COMM_FILE, "w", encoding="utf-8") as f:
        f.write(comm_content)
    print("[1/3] '.comm' 파일 생성 완료")


def generate_export_file() -> None:
    export_content = f"""P actions make_etude
P debug nodebug
P mode interactif
P ncpus 1
P nomjob analysis
P time_limit {TIMEOUT_SEC}.0
P version stable
A args
A memjeveux 1024.0
A tpmax {TIMEOUT_SEC}.0
F comm {COMM_FILE} D  1
F mmed {MED_FILE} D  20
F mess {MESS_FILE} R  6
F resu {RESU_FILE} R  8
F rmed {RMED_FILE} R  80
"""
    with open(EXPORT_FILE, "w", encoding="utf-8") as f:
        f.write(export_content)
    print("[2/3] '.export' 프로파일 생성 완료")


def write_runner_script() -> None:
    safe_current_dir = CURRENT_DIR.replace('\\', '/')
    safe_export_file = EXPORT_FILE.replace('\\', '/')

    runner_content = f"""import os
import subprocess
import sys
import traceback

log_path = "{safe_current_dir}/aster_execution_log.txt"

def log(msg):
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(str(msg) + "\\n")

try:
    current_dir = "{safe_current_dir}"
    os.chdir(current_dir)

    export_file_path = "{safe_export_file}"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("=== EXECUTION LOG ===\\n")

    log("[Salome Batch] Code_Aster 해석 엔진(as_run) 구동 준비...")
    log(f"대상 프로파일: {{export_file_path}}")

    cmd = f'as_run "{{export_file_path}}"'
    log(f"실행 명령: {{cmd}}")

    res = subprocess.run(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding='utf-8', errors='replace'
    )

    log("=== as_run STDOUT/STDERR ===")
    log(res.stdout)
    log(f"=== returncode: {{res.returncode}} ===")

    sys.exit(res.returncode)

except Exception:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\\n=== UNCAUGHT EXCEPTION ===\\n")
        f.write(traceback.format_exc())
    print("=== UNCAUGHT EXCEPTION ===")
    traceback.print_exc()
    sys.exit(1)
"""
    with open(RUNNER_SCRIPT, "w", encoding="utf-8") as f:
        f.write(runner_content)
    print("[3/3] 배치 실행용 러너 스크립트 생성 완료")


def run_salome_batch() -> int:
    """Salome-Meca를 배치 모드로 실행. 임시 배치 파일을 생성하여 윈도우 따옴표 버그를 원천 차단합니다."""
    # 1. 윈도우 따옴표 꼬임 방지를 위해 임시 실행 파일(.bat) 경로 지정
    temp_bat = os.path.join(CURRENT_DIR, "run_aster_temp.bat")

    # 2. 확실하게 작동하는 순수 윈도우 cmd 명령어를 임시 파일에 작성
    with open(temp_bat, "w", encoding="utf-8") as f:
        f.write("@echo off\n")
        f.write(f'call "{SALOME_BAT}" -t "{RUNNER_SCRIPT}"\n')

    print("\n해석 구동 중... (완료까지 시간이 걸릴 수 있습니다)")
    print(f"[DEBUG] 임시 배치 파일로 실행 우회: {temp_bat}")

    try:
        # 3. 파이썬은 단순히 따옴표 충돌이 없는 임시 파일 하나만 실행
        result = subprocess.run(
            f'"{temp_bat}"',
            shell=True,
            capture_output=True,
            text=True,
            cwd=CURRENT_DIR,
            timeout=TIMEOUT_SEC,
        )

        # 4. 실행이 끝난 후 찌꺼기 파일(임시 배치 파일) 삭제
        if os.path.exists(temp_bat):
            os.remove(temp_bat)

        print("\n--- run_salome.bat stdout/stderr ---")
        print(result.stdout or "(stdout 없음)")
        if result.stderr:
            print("--- stderr ---")
            print(result.stderr)

        return result.returncode

    except subprocess.TimeoutExpired as e:
        print(f"\n[TIMEOUT] {TIMEOUT_SEC}초 내에 해석이 끝나지 않았습니다.")
        # 타임아웃 시에도 임시 파일 삭제
        if os.path.exists(temp_bat):
            os.remove(temp_bat)
        show_notification(
            "Code_Aster 해석 타임아웃",
            f"{TIMEOUT_SEC}초 내에 해석이 끝나지 않았습니다.",
            is_error=True,
        )
        return -1
    except Exception as e:
        print(f"\n[ERROR] 파이썬 서브프로세스 실행 오류: {e}")
        show_notification(
            "Code_Aster 실행 오류",
            f"서브프로세스 실행 중 오류가 발생했습니다.\n\n{e}",
            is_error=True,
        )
        return -1


def check_result(returncode: int) -> bool:
    success = True
    if returncode != 0:
        print(f"\n[FAIL] 프로세스 종료 코드가 0이 아닙니다 (returncode={returncode}).")
        success = False

    if not os.path.exists(MESS_FILE):
        print("\n[FATAL] analysis.mess 파일이 생성되지 않았습니다.")
        show_notification("Code_Aster 해석 실패", "analysis.mess 파일이 생성되지 않았습니다.", is_error=True)
        return False

    with open(MESS_FILE, "r", encoding="utf-8", errors="replace") as f:
        mess_lines = f.readlines()
    mess_text_lower = "".join(mess_lines).lower()

    if any(marker in mess_text_lower for marker in ASTER_ERROR_MARKERS):
        print("\n[Aster ERROR] .mess 파일에서 에러 마커가 발견되었습니다. 마지막 30줄:")
        print("".join(mess_lines[-30:]))
        success = False
    elif ASTER_SUCCESS_MARKER.lower() not in mess_text_lower:
        print("\n[WARNING] .mess 파일에서 명시적 성공 마커를 찾지 못했습니다.")
        success = False

    for path, label in ((RMED_FILE, "rmed"), (RESU_FILE, "resu")):
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            print(f"\n[WARNING] {label} 결과 파일이 없거나 비어 있습니다: {path}")
            success = False

    if success:
        print("\n[SUCCESS] Code_Aster 해석이 성공적으로 완료되었습니다!")
        print(f" -> 3D 결과 파일(ParaViS용): {RMED_FILE}")
        show_notification(
            "Code_Aster 해석 완료",
            f"해석이 성공적으로 완료되었습니다.\n\n결과 파일:\n{RMED_FILE}",
            is_error=False,
        )
    else:
        show_notification(
            "Code_Aster 해석 실패/경고",
            "해석이 정상적으로 완료되지 않았습니다.\n자세한 내용은 콘솔 로그와 analysis.mess를 확인하세요.",
            is_error=True,
        )

    return success


def print_execution_log() -> None:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            print("\n--- aster_execution_log.txt 내용 ---")
            print(f.read())


def main() -> int:
    start_time = time.time()

    check_prerequisites()
    generate_comm_file()
    generate_export_file()
    write_runner_script()

    returncode = run_salome_batch()
    ok = check_result(returncode)
    print_execution_log()

    elapsed = time.time() - start_time
    print(f"\n[INFO] 총 소요 시간: {elapsed/60:.1f}분")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())