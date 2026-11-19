#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
salome_auto_pipeline.py
=======================
Salome-Meca 실행 -> MED 파일 생성/저장 감지(또는 창 종료 대기) -> Code_Aster 자동 해석 -> PDF 리포트 생성 통합 프로그램

사용법:
  1) GUI 모드 (기본):
     python salome_auto_pipeline.py

  2) CLI 자동 실행 (Salome GUI 실행 후 MED 저장/종료 시 자동 해석 & 리포트):
     python salome_auto_pipeline.py --cli

  3) 기존 MED 파일로 즉시 해석 + 리포트만 실행 (Salome GUI 생략):
     python salome_auto_pipeline.py --cli --skip-salome

  4) 기존 RMED 파일로 리포트만 즉시 재생성:
     python salome_auto_pipeline.py --cli --report-only
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import h5py
import numpy as np

import praview_report

CURRENT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = CURRENT_DIR / "automation_config.json"

ASTER_SUCCESS_MARKER = "arret normal"
ASTER_ERROR_MARKERS = ("<fatal alarm>", "<exception>", "erreur", "<f>", "arret sur erreur")


# ==============================================================================
# 1. 설정(Config) 로드 및 저장
# ==============================================================================
def get_default_config() -> dict:
    return {
        "salome_gui_bat": r"C:\Users\ghchoi\Desktop\v2023\run_mesa_salome.bat",
        "salome_batch_bat": r"C:\Users\ghchoi\Desktop\v2023\run_salome.bat",
        "med_file": str(CURRENT_DIR / "mesh.med"),
        "output_dir": str(CURRENT_DIR / "report_out"),
        "auto_detect_med_on_save": True,
        "auto_open_pdf": True,
        "timeout_sec": 3600,
        "material": {
            "name": "Structural Steel",
            "E_GPa": 200.0,
            "nu": 0.3,
            "rho": 7850.0,
            "yield_MPa": 250.0,
        },
        "boundary_conditions": {
            "fix_group": "fix",
            "load_group": "load_z",
            "load_type": "FORCE_FACE",  # FORCE_FACE | PRES_REP | FORCE_NODALE
            "fx": 0.0,
            "fy": 0.0,
            "fz": -50000000.0,
        },
        "report": {
            "lang": "ko",
            "length_unit": "m",
            "stress_unit": "Pa",
            "scale": "1",
            "az": -60.0,
            "el": 25.0,
        },
    }


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    cfg = get_default_config()
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[WARN] 설정 파일 로드 실패 ({config_path}): {e}")
    else:
        save_config(cfg, config_path)
    return cfg


def save_config(cfg: dict, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ==============================================================================
# 2. MED 파일 검사 유틸리티 (그룹 및 요소 확인)
# ==============================================================================
def inspect_med_file(med_path: str | Path) -> dict:
    """MED 파일 내부의 노드 수, 요소 종류, 그룹(Family) 목록을 읽어 반환합니다."""
    med_path = Path(med_path)
    if not med_path.exists():
        raise FileNotFoundError(f"MED 파일을 찾을 수 없습니다: {med_path}")

    info = {"mesh_name": "", "nodes": 0, "elements": {}, "groups": []}
    with h5py.File(med_path, "r") as f:
        if "ENS_MAA" not in f:
            raise ValueError("유효한 MED 메쉬 구조(ENS_MAA)가 없습니다.")
        mesh_names = list(f["ENS_MAA"].keys())
        if not mesh_names:
            raise ValueError("MED 파일에 메쉬가 없습니다.")
        mname = mesh_names[0]
        info["mesh_name"] = mname
        mg = f["ENS_MAA"][mname]
        mstep = mg[sorted(mg.keys())[0]]
        if "NOE" in mstep and "COO" in mstep["NOE"]:
            info["nodes"] = int(mstep["NOE/COO"].attrs.get("NBR", 0))

        if "MAI" in mstep:
            for etype in mstep["MAI"].keys():
                g = mstep["MAI"][etype]
                if "FAM" in g:
                    info["elements"][etype] = int(g["FAM"].shape[0])

        groups = set()
        if "FAS" in f and mname in f["FAS"]:
            fas = f["FAS"][mname]
            for kind in ("ELEME", "NOEUD"):
                if kind not in fas:
                    continue
                for fname in fas[kind].keys():
                    fg = fas[kind][fname]
                    if "GRO" in fg and "NOM" in fg["GRO"]:
                        for row in np.atleast_2d(fg["GRO/NOM"][:]):
                            s = bytes(row.astype(np.uint8)).decode("utf-8", "ignore").strip("\x00 ").strip()
                            if s:
                                groups.add(s)
        info["groups"] = sorted(groups)
    return info


# ==============================================================================
# 3. 파이프라인 핵심 엔진 (Salome 실행 / Code_Aster 해석 / PDF 리포트)
# ==============================================================================
def show_notification(title: str, message: str, is_error: bool = False) -> None:
    try:
        icon = 0x10 if is_error else 0x40
        MB_TOPMOST = 0x40000
        ctypes.windll.user32.MessageBoxW(0, message, title, icon | MB_TOPMOST)
    except Exception:
        pass


class AutomationPipeline:
    def __init__(self, cfg: dict, logger: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.log_fn = logger or (lambda msg: print(msg, flush=True))
        self.work_dir = CURRENT_DIR
        self.comm_file = self.work_dir / "analysis.comm"
        self.export_file = self.work_dir / "run_case.export"
        self.mess_file = self.work_dir / "analysis.mess"
        self.resu_file = self.work_dir / "analysis.resu"
        self.rmed_file = self.work_dir / "result.rmed"
        self.runner_script = self.work_dir / "run_aster_batch.py"
        self.exec_log_file = self.work_dir / "aster_execution_log.txt"

    def log(self, msg: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.log_fn(f"[{ts}] {msg}")

    # --------------------------------------------------------------------------
    # Step 1: Salome GUI 실행 및 MED 파일 생성 대기
    # --------------------------------------------------------------------------
    def launch_salome_and_wait_for_med(
        self,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[Path]:
        salome_gui_bat = Path(self.cfg["salome_gui_bat"])
        if not salome_gui_bat.exists():
            fallback = Path(self.cfg["salome_batch_bat"])
            if fallback.exists():
                salome_gui_bat = fallback
            else:
                raise FileNotFoundError(f"Salome 실행 파일을 찾을 수 없습니다: {salome_gui_bat}")

        target_med = Path(self.cfg["med_file"]).resolve()
        watch_dir = target_med.parent
        watch_dir.mkdir(parents=True, exist_ok=True)
        auto_detect_on_save = bool(self.cfg.get("auto_detect_med_on_save", True))

        # 실행 전 폴더 내 기존 .med 파일들의 수정 시각 기록
        initial_mtimes: dict[Path, float] = {}
        for p in watch_dir.glob("*.med"):
            try:
                initial_mtimes[p.resolve()] = p.stat().st_mtime
            except OSError:
                pass
        if target_med.exists():
            initial_mtimes[target_med] = target_med.stat().st_mtime

        start_ts = time.time()
        self.log(f"Salome-Meca GUI를 실행합니다: {salome_gui_bat}")
        if auto_detect_on_save:
            self.log(
                f"대기 중... Salome에서 MED 파일({target_med.name} 또는 폴더 내 *.med)을 저장하거나 "
                f"Salome 창을 닫으면 자동으로 해석이 시작됩니다."
            )
        else:
            self.log("대기 중... Salome에서 MED 파일을 저장하고 Salome 창을 닫으면 해석이 시작됩니다.")

        proc = subprocess.Popen(
            f'"{salome_gui_bat}"',
            shell=True,
            cwd=str(self.work_dir),
        )

        detected_med: Optional[Path] = None

        while True:
            if stop_event and stop_event.is_set():
                self.log("사용자 요청으로 대기를 중단합니다.")
                return None

            ret = proc.poll()

            # 폴더 내 새로 생성되거나 갱신된 .med 파일 확인
            updated_candidates: list[tuple[float, Path]] = []
            candidates = list(watch_dir.glob("*.med"))
            if target_med.exists() and target_med not in candidates:
                candidates.append(target_med)

            for p in candidates:
                try:
                    rp = p.resolve()
                    st = rp.stat()
                    prev_mtime = initial_mtimes.get(rp, 0.0)
                    if st.st_mtime > max(prev_mtime, start_ts) and st.st_size > 0:
                        updated_candidates.append((st.st_mtime, rp))
                except OSError:
                    continue

            if updated_candidates and auto_detect_on_save:
                updated_candidates.sort(key=lambda x: x[0], reverse=True)
                cand = updated_candidates[0][1]
                # 파일 쓰기가 완전히 끝날 때까지 크기 안정화 대기 (최대 5초)
                self.log(f"MED 파일 저장 감지됨: {cand.name} (파일 쓰기 완료 확인 중...)")
                if self._wait_file_stable(cand):
                    detected_med = cand
                    self.log(f"MED 파일 준비 완료: {detected_med}")
                    break

            if ret is not None:
                self.log("Salome 프로세스 종료가 감지되었습니다.")
                if updated_candidates:
                    updated_candidates.sort(key=lambda x: x[0], reverse=True)
                    detected_med = updated_candidates[0][1]
                    self.log(f"새로 저장된 MED 파일을 사용합니다: {detected_med}")
                elif target_med.exists():
                    detected_med = target_med
                    self.log(f"기존 설정된 MED 파일을 사용합니다: {detected_med}")
                else:
                    self.log(f"[오류] MED 파일을 찾을 수 없습니다: {target_med}")
                    return None
                break

            time.sleep(1.0)

        if detected_med:
            self.cfg["med_file"] = str(detected_med)
        return detected_med

    @staticmethod
    def _wait_file_stable(path: Path, wait_sec: float = 1.5, retries: int = 10) -> bool:
        last_size = -1
        for _ in range(retries):
            try:
                sz = path.stat().st_size
                if sz > 0 and sz == last_size:
                    # HDF5/MED 파일이 정상적으로 열리는지 검증
                    with h5py.File(path, "r") as f:
                        if "ENS_MAA" in f:
                            return True
                last_size = sz
            except Exception:
                pass
            time.sleep(wait_sec)
        return False

    # --------------------------------------------------------------------------
    # Step 2: Code_Aster 입력 파일(.comm, .export, runner) 생성
    # --------------------------------------------------------------------------
    def generate_comm_file(self) -> Path:
        mat = self.cfg["material"]
        bc = self.cfg["boundary_conditions"]
        rep = self.cfg["report"]

        stress_unit = rep.get("stress_unit", "Pa")
        s2mpa = praview_report.STRESS_TO_MPA.get(stress_unit, 1e-6)
        # E_GPa (GPa = 1000 MPa) -> 모델의 응력 단위로 변환 (예: Pa 단위가 기본이면 200 GPa = 2.0e11 Pa)
        E_model = float(mat["E_GPa"]) * 1000.0 / s2mpa
        nu = float(mat["nu"])
        rho = float(mat.get("rho", 7850.0))

        fix_group = bc["fix_group"].strip()
        load_group = bc["load_group"].strip()
        load_type = bc.get("load_type", "FORCE_FACE").strip().upper()
        fx = float(bc.get("fx", 0.0))
        fy = float(bc.get("fy", 0.0))
        fz = float(bc.get("fz", -50000000.0))

        # 하중 항목 생성
        force_components = []
        if fx != 0.0:
            force_components.append(f"FX={fx}")
        if fy != 0.0:
            force_components.append(f"FY={fy}")
        if fz != 0.0 or not force_components:
            force_components.append(f"FZ={fz}")
        force_args_str = ",\n                                     ".join(force_components)

        if load_type == "PRES_REP":
            # PRES_REP일 경우 fz를 압력(PRES) 값으로 사용
            load_block = f"""load0 = AFFE_CHAR_MECA(PRES_REP=_F(PRES={fz},
                                   GROUP_MA='{load_group}'),
                       MODELE=model)"""
        elif load_type == "FORCE_NODALE":
            load_block = f"""load0 = AFFE_CHAR_MECA(FORCE_NODALE=_F({force_args_str},
                                       GROUP_NO='{load_group}'),
                       MODELE=model)"""
        else:
            load_block = f"""load0 = AFFE_CHAR_MECA(FORCE_FACE=_F({force_args_str},
                                     GROUP_MA='{load_group}'),
                       MODELE=model)"""

        comm_content = f"""DEBUT(LANG='EN')

mesh = LIRE_MAILLAGE(UNITE=20,
                     VERI_MAIL=_F(VERIF='OUI'))

model = AFFE_MODELE(AFFE=_F(MODELISATION='3D',
                            PHENOMENE='MECANIQUE',
                            TOUT='OUI'),
                    MAILLAGE=mesh)

mater = DEFI_MATERIAU(ELAS=_F(E={E_model},
                              NU={nu},
                              RHO={rho}))

fieldmat = AFFE_MATERIAU(AFFE=_F(MATER=mater,
                                 TOUT='OUI'),
                         MAILLAGE=mesh,
                         MODELE=model)

fix = AFFE_CHAR_MECA(DDL_IMPO=_F(DX=0.0,
                                 DY=0.0,
                                 DZ=0.0,
                                 GROUP_MA='{fix_group}'),
                     MODELE=model)

{load_block}

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
        self.comm_file.write_text(comm_content, encoding="utf-8")
        self.log(f"[1/4] Code_Aster 명령 파일(.comm) 생성 완료: {self.comm_file.name}")
        return self.comm_file

    def generate_export_file(self) -> Path:
        timeout_sec = int(self.cfg.get("timeout_sec", 3600))
        med_file = os.path.normpath(self.cfg["med_file"])

        export_content = f"""P actions make_etude
P debug nodebug
P mode interactif
P ncpus 1
P nomjob analysis
P time_limit {timeout_sec}.0
P version stable
A args
A memjeveux 1024.0
A tpmax {timeout_sec}.0
F comm {os.path.normpath(self.comm_file)} D  1
F mmed {med_file} D  20
F mess {os.path.normpath(self.mess_file)} R  6
F resu {os.path.normpath(self.resu_file)} R  8
F rmed {os.path.normpath(self.rmed_file)} R  80
"""
        self.export_file.write_text(export_content, encoding="utf-8")
        self.log(f"[2/4] Code_Aster 실행 프로파일(.export) 생성 완료: {self.export_file.name}")
        return self.export_file

    def write_runner_script(self) -> Path:
        safe_current_dir = str(self.work_dir).replace("\\", "/")
        safe_export_file = str(self.export_file).replace("\\", "/")
        safe_log_path = str(self.exec_log_file).replace("\\", "/")

        runner_content = f"""import os
import subprocess
import sys
import traceback

log_path = "{safe_log_path}"

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
        self.runner_script.write_text(runner_content, encoding="utf-8")
        return self.runner_script

    # --------------------------------------------------------------------------
    # Step 3: Code_Aster 배치 해석 실행 및 검증
    # --------------------------------------------------------------------------
    def run_aster_analysis(self) -> bool:
        med_path = Path(self.cfg["med_file"])
        if not med_path.exists():
            raise FileNotFoundError(f"해석할 MED 파일이 없습니다: {med_path}")

        salome_bat = Path(self.cfg["salome_batch_bat"])
        if not salome_bat.exists():
            raise FileNotFoundError(f"Salome 배치 실행 파일이 없습니다: {salome_bat}")

        # MED 파일 사전 검사 (그룹 확인 경고)
        try:
            med_info = inspect_med_file(med_path)
            self.log(
                f"MED 메쉬 확인: 노드={med_info['nodes']:,}개, "
                f"요소={med_info['elements']}, 그룹={med_info['groups']}"
            )
            bc = self.cfg["boundary_conditions"]
            for gkey, glabel in (("fix_group", "고정 그룹"), ("load_group", "하중 그룹")):
                gname = bc[gkey]
                if med_info["groups"] and gname not in med_info["groups"]:
                    self.log(
                        f"[경고] 설정된 {glabel} '{gname}'이(가) MED 파일 그룹 목록 {med_info['groups']}에 없습니다!"
                    )
        except Exception as e:
            self.log(f"[참고] MED 사전 검사 건너뜀: {e}")

        self.generate_comm_file()
        self.generate_export_file()
        self.write_runner_script()

        # 이전 결과 파일 초기화 (성공 여부를 명확히 판별하기 위함)
        for old_file in (self.mess_file, self.rmed_file, self.resu_file):
            if old_file.exists():
                try:
                    old_file.unlink()
                except OSError:
                    pass

        temp_bat = self.work_dir / "run_aster_temp.bat"
        temp_bat.write_text(
            f'@echo off\ncall "{salome_bat}" -t "{self.runner_script}"\n',
            encoding="utf-8",
        )

        timeout_sec = int(self.cfg.get("timeout_sec", 3600))
        self.log("[3/4] Code_Aster 배치 해석을 시작합니다... (모델 크기에 따라 수십 초~수 분 소요)")
        t0 = time.time()

        try:
            result = subprocess.run(
                f'"{temp_bat}"',
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(self.work_dir),
                timeout=timeout_sec,
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            self.log(f"[타임아웃] {timeout_sec}초 내에 해석이 완료되지 않았습니다.")
            return False
        finally:
            if temp_bat.exists():
                try:
                    temp_bat.unlink()
                except OSError:
                    pass

        elapsed = time.time() - t0
        ok = self._verify_aster_result(returncode)
        if ok:
            self.log(f"Code_Aster 해석 완료! (소요 시간: {elapsed:.1f}초) -> {self.rmed_file.name}")
        else:
            self.log(f"[실패] Code_Aster 해석 중 오류가 발생했습니다. ({self.mess_file.name} 확인)")
        return ok

    def _verify_aster_result(self, returncode: int) -> bool:
        success = True
        if returncode != 0:
            self.log(f"[경고] 프로세스 종료 코드가 0이 아닙니다 (returncode={returncode}).")
            success = False

        if not self.mess_file.exists():
            self.log("[오류] analysis.mess 파일이 생성되지 않았습니다.")
            if self.exec_log_file.exists():
                self.log(self.exec_log_file.read_text(encoding="utf-8", errors="replace")[-1500:])
            return False

        mess_lines = self.mess_file.read_text(encoding="utf-8", errors="replace").splitlines()
        mess_lower = "\n".join(mess_lines).lower()

        if any(marker in mess_lower for marker in ASTER_ERROR_MARKERS):
            self.log("[Aster 오류] .mess 파일에서 에러 마커가 발견되었습니다. 마지막 25줄:")
            for line in mess_lines[-25:]:
                self.log("  " + line)
            success = False
        elif ASTER_SUCCESS_MARKER.lower() not in mess_lower:
            self.log("[경고] .mess 파일에서 정상 종료 마커(ARRET NORMAL)를 찾지 못했습니다.")
            success = False

        if not self.rmed_file.exists() or self.rmed_file.stat().st_size == 0:
            self.log(f"[오류] 결과 파일(.rmed)이 생성되지 않았습니다: {self.rmed_file}")
            success = False

        return success

    # --------------------------------------------------------------------------
    # Step 4: PDF 리포트 및 이미지 자동 생성 (praview_report 연동)
    # --------------------------------------------------------------------------
    def generate_pdf_report(self, rmed_path: Optional[Path] = None) -> Path:
        rmed = Path(rmed_path or self.rmed_file)
        if not rmed.exists():
            raise FileNotFoundError(f"리포트를 생성할 .rmed 파일이 없습니다: {rmed}")

        mat = self.cfg["material"]
        rep = self.cfg["report"]
        out_dir = Path(self.cfg["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)

        argv = [
            str(rmed),
            "-o", str(out_dir),
            "--lang", str(rep.get("lang", "ko")),
            "--mat-name", str(mat.get("name", "Structural Steel")),
            "--E", str(mat.get("E_GPa", 200.0)),
            "--nu", str(mat.get("nu", 0.3)),
            "--rho", str(mat.get("rho", 7850.0)),
            "--sy", str(mat.get("yield_MPa", 250.0)),
            "--length-unit", str(rep.get("length_unit", "m")),
            "--stress-unit", str(rep.get("stress_unit", "Pa")),
            "--scale", str(rep.get("scale", "1")),
            "--az", str(rep.get("az", -60.0)),
            "--el", str(rep.get("el", 25.0)),
        ]
        if self.comm_file.exists():
            argv.extend(["--comm", str(self.comm_file)])

        self.log("[4/4] 결과 데이터 분석 및 PDF 리포트 생성을 시작합니다...")
        pdf_path = Path(praview_report.main(argv))
        self.log(f"PDF 리포트 생성 완료: {pdf_path}")

        # 요약 통계 로그 출력
        summary_file = out_dir / "summary.json"
        if summary_file.exists():
            try:
                s = json.loads(summary_file.read_text(encoding="utf-8"))
                u_max = s["displacement_mm"]["|U|"]["max"]
                vm_max = s["von_mises_MPa"]["max"]
                sf = s["safety_factor"]
                self.log(
                    f"[해석 요약] 최대 변위 |U| = {u_max:,.3f} mm | "
                    f"최대 von Mises 응력 = {vm_max:,.2f} MPa | "
                    f"안전율(SF) = {sf:.3f}"
                )
            except Exception:
                pass

        if self.cfg.get("auto_open_pdf", True) and pdf_path.exists():
            try:
                os.startfile(str(pdf_path))
                self.log("생성된 PDF 리포트를 열었습니다.")
            except Exception as e:
                self.log(f"[참고] PDF 자동 열기 실패: {e}")

        return pdf_path


# ==============================================================================
# 4. 통합 GUI 애플리케이션 (Tkinter)
# ==============================================================================
def run_gui(cfg: dict) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title("Salome-Meca 자동 해석 및 리포트 생성기 (MED -> Code_Aster -> PDF Report)")
    root.geometry("860x780")
    root.minsize(820, 720)

    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")

    stop_event = threading.Event()
    is_running = False

    # --- Tkinter 변수 바인딩 ---
    v_salome_gui = tk.StringVar(value=cfg["salome_gui_bat"])
    v_salome_batch = tk.StringVar(value=cfg["salome_batch_bat"])
    v_med_file = tk.StringVar(value=cfg["med_file"])
    v_out_dir = tk.StringVar(value=cfg["output_dir"])
    v_auto_detect = tk.BooleanVar(value=bool(cfg.get("auto_detect_med_on_save", True)))
    v_auto_open = tk.BooleanVar(value=bool(cfg.get("auto_open_pdf", True)))

    mat = cfg["material"]
    v_mat_name = tk.StringVar(value=str(mat.get("name", "Structural Steel")))
    v_E = tk.StringVar(value=str(mat.get("E_GPa", 200.0)))
    v_nu = tk.StringVar(value=str(mat.get("nu", 0.3)))
    v_rho = tk.StringVar(value=str(mat.get("rho", 7850.0)))
    v_sy = tk.StringVar(value=str(mat.get("yield_MPa", 250.0)))

    bc = cfg["boundary_conditions"]
    v_fix_grp = tk.StringVar(value=str(bc.get("fix_group", "fix")))
    v_load_grp = tk.StringVar(value=str(bc.get("load_group", "load_z")))
    v_load_type = tk.StringVar(value=str(bc.get("load_type", "FORCE_FACE")))
    v_fx = tk.StringVar(value=str(bc.get("fx", 0.0)))
    v_fy = tk.StringVar(value=str(bc.get("fy", 0.0)))
    v_fz = tk.StringVar(value=str(bc.get("fz", -50000000.0)))

    rep = cfg["report"]
    v_lang = tk.StringVar(value=str(rep.get("lang", "ko")))
    v_len_unit = tk.StringVar(value=str(rep.get("length_unit", "m")))
    v_str_unit = tk.StringVar(value=str(rep.get("stress_unit", "Pa")))
    v_scale = tk.StringVar(value=str(rep.get("scale", "1")))
    v_az = tk.StringVar(value=str(rep.get("az", -60.0)))
    v_el = tk.StringVar(value=str(rep.get("el", 25.0)))

    def sync_ui_to_cfg() -> dict:
        cfg["salome_gui_bat"] = v_salome_gui.get().strip()
        cfg["salome_batch_bat"] = v_salome_batch.get().strip()
        cfg["med_file"] = v_med_file.get().strip()
        cfg["output_dir"] = v_out_dir.get().strip()
        cfg["auto_detect_med_on_save"] = bool(v_auto_detect.get())
        cfg["auto_open_pdf"] = bool(v_auto_open.get())

        cfg["material"] = {
            "name": v_mat_name.get().strip() or "Structural Steel",
            "E_GPa": float(v_E.get()),
            "nu": float(v_nu.get()),
            "rho": float(v_rho.get()),
            "yield_MPa": float(v_sy.get()),
        }
        cfg["boundary_conditions"] = {
            "fix_group": v_fix_grp.get().strip(),
            "load_group": v_load_grp.get().strip(),
            "load_type": v_load_type.get().strip(),
            "fx": float(v_fx.get()),
            "fy": float(v_fy.get()),
            "fz": float(v_fz.get()),
        }
        cfg["report"] = {
            "lang": v_lang.get().strip(),
            "length_unit": v_len_unit.get().strip(),
            "stress_unit": v_str_unit.get().strip(),
            "scale": v_scale.get().strip() or "1",
            "az": float(v_az.get()),
            "el": float(v_el.get()),
        }
        save_config(cfg)
        return cfg

    # --- UI 레이아웃 구성 ---
    main_frame = ttk.Frame(root, padding=12)
    main_frame.pack(fill=tk.BOTH, expand=True)

    # 1) 경로 설정 프레임
    f_paths = ttk.LabelFrame(main_frame, text=" 1. 경로 및 실행 옵션 ", padding=8)
    f_paths.pack(fill=tk.X, pady=(0, 8))
    f_paths.columnconfigure(1, weight=1)

    def add_path_row(parent, row, label, var, is_dir=False, filetypes=None):
        ttk.Label(parent, text=label, width=18).grid(row=row, column=0, sticky=tk.W, pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky=tk.EW, padx=5, pady=2)

        def browse():
            if is_dir:
                p = filedialog.askdirectory(initialdir=var.get() or str(CURRENT_DIR))
            else:
                p = filedialog.askopenfilename(
                    initialdir=str(Path(var.get() or CURRENT_DIR).parent),
                    filetypes=filetypes or [("All Files", "*.*")],
                )
            if p:
                var.set(os.path.normpath(p))

        ttk.Button(parent, text="찾아보기...", width=10, command=browse).grid(row=row, column=2, pady=2)

    add_path_row(f_paths, 0, "Salome GUI (.bat):", v_salome_gui, filetypes=[("Batch Files", "*.bat")])
    add_path_row(f_paths, 1, "Salome Batch (.bat):", v_salome_batch, filetypes=[("Batch Files", "*.bat")])
    add_path_row(f_paths, 2, "대상 MED 파일:", v_med_file, filetypes=[("MED Files", "*.med"), ("All Files", "*.*")])
    add_path_row(f_paths, 3, "리포트 출력 폴더:", v_out_dir, is_dir=True)

    chk_frame = ttk.Frame(f_paths)
    chk_frame.grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))
    ttk.Checkbutton(
        chk_frame,
        text="Salome 실행 중 MED 파일 저장 시 즉시 자동 감지하여 해석 시작 (체크 해제 시 Salome 창을 닫을 때 시작)",
        variable=v_auto_detect,
    ).pack(side=tk.LEFT, padx=(0, 16))
    ttk.Checkbutton(chk_frame, text="완료 후 PDF 자동 열기", variable=v_auto_open).pack(side=tk.LEFT)

    # 2) 재질 및 경계조건 프레임 (좌우 배치)
    mid_frame = ttk.Frame(main_frame)
    mid_frame.pack(fill=tk.X, pady=(0, 8))
    mid_frame.columnconfigure(0, weight=1)
    mid_frame.columnconfigure(1, weight=1)

    # 2-A) 재질 정보
    f_mat = ttk.LabelFrame(mid_frame, text=" 2. 재질 설정 (Material) ", padding=8)
    f_mat.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))
    f_mat.columnconfigure(1, weight=1)

    mat_fields = [
        ("재질 이름:", v_mat_name),
        ("탄성계수 E [GPa]:", v_E),
        ("푸아송 비 nu [-]:", v_nu),
        ("밀도 rho [kg/m³]:", v_rho),
        ("항복강도 Sy [MPa]:", v_sy),
    ]
    for i, (lbl, var) in enumerate(mat_fields):
        ttk.Label(f_mat, text=lbl, width=18).grid(row=i, column=0, sticky=tk.W, pady=2)
        ttk.Entry(f_mat, textvariable=var).grid(row=i, column=1, sticky=tk.EW, pady=2)

    # 2-B) 경계조건 및 하중
    f_bc = ttk.LabelFrame(mid_frame, text=" 3. 경계조건 및 하중 (Boundary Conditions) ", padding=8)
    f_bc.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))
    f_bc.columnconfigure(1, weight=1)

    ttk.Label(f_bc, text="고정 그룹 (DX=DY=DZ=0):", width=22).grid(row=0, column=0, sticky=tk.W, pady=2)
    fix_cb = ttk.Combobox(f_bc, textvariable=v_fix_grp)
    fix_cb.grid(row=0, column=1, sticky=tk.EW, pady=2)

    ttk.Label(f_bc, text="하중 적용 그룹:", width=22).grid(row=1, column=0, sticky=tk.W, pady=2)
    load_cb = ttk.Combobox(f_bc, textvariable=v_load_grp)
    load_cb.grid(row=1, column=1, sticky=tk.EW, pady=2)

    ttk.Label(f_bc, text="하중 종류:", width=22).grid(row=2, column=0, sticky=tk.W, pady=2)
    type_cb = ttk.Combobox(
        f_bc, textvariable=v_load_type, values=["FORCE_FACE", "PRES_REP", "FORCE_NODALE"], state="readonly"
    )
    type_cb.grid(row=2, column=1, sticky=tk.EW, pady=2)

    f_xyz = ttk.Frame(f_bc)
    f_xyz.grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=2)
    for idx, (lbl, var) in enumerate([("FX:", v_fx), ("FY:", v_fy), ("FZ (또는 PRES):", v_fz)]):
        ttk.Label(f_xyz, text=lbl).grid(row=0, column=idx * 2, sticky=tk.W, padx=(0 if idx == 0 else 8, 2))
        ttk.Entry(f_xyz, textvariable=var, width=13).grid(row=0, column=idx * 2 + 1, sticky=tk.EW)
        f_xyz.columnconfigure(idx * 2 + 1, weight=1)

    def load_groups_from_med():
        med_p = Path(v_med_file.get().strip())
        if not med_p.exists():
            messagebox.showwarning("MED 파일 없음", f"MED 파일이 존재하지 않습니다:\n{med_p}")
            return
        try:
            info = inspect_med_file(med_p)
            grps = info["groups"]
            fix_cb["values"] = grps
            load_cb["values"] = grps
            append_log(
                f"[MED 검사] 메쉬='{info['mesh_name']}', 노드={info['nodes']:,}개, "
                f"요소={info['elements']}, 그룹={grps}"
            )
            if grps:
                messagebox.showinfo(
                    "MED 그룹 불러오기 완료",
                    f"메쉬 이름: {info['mesh_name']}\n"
                    f"노드 수: {info['nodes']:,}\n"
                    f"요소: {info['elements']}\n"
                    f"감지된 그룹: {', '.join(grps)}",
                )
            else:
                messagebox.showinfo("MED 검사", "MED 파일에 정의된 그룹(Family)이 없습니다.")
        except Exception as e:
            messagebox.showerror("MED 읽기 오류", str(e))

    ttk.Button(f_bc, text="현재 MED 파일에서 그룹 목록 불러오기", command=load_groups_from_med).grid(
        row=4, column=0, columnspan=2, sticky=tk.EW, pady=(6, 0)
    )

    # 3) 리포트 옵션 프레임
    f_rep = ttk.LabelFrame(main_frame, text=" 4. PDF 리포트 옵션 ", padding=8)
    f_rep.pack(fill=tk.X, pady=(0, 8))

    ttk.Label(f_rep, text="언어:").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
    ttk.Combobox(f_rep, textvariable=v_lang, values=["ko", "en"], width=6, state="readonly").grid(
        row=0, column=1, padx=(0, 14)
    )

    ttk.Label(f_rep, text="길이 단위:").grid(row=0, column=2, sticky=tk.W, padx=(0, 4))
    ttk.Combobox(
        f_rep, textvariable=v_len_unit, values=list(praview_report.LENGTH_TO_MM.keys()), width=6, state="readonly"
    ).grid(row=0, column=3, padx=(0, 14))

    ttk.Label(f_rep, text="응력 단위:").grid(row=0, column=4, sticky=tk.W, padx=(0, 4))
    ttk.Combobox(
        f_rep, textvariable=v_str_unit, values=list(praview_report.STRESS_TO_MPA.keys()), width=6, state="readonly"
    ).grid(row=0, column=5, padx=(0, 14))

    ttk.Label(f_rep, text="변형 배율(숫자/auto):").grid(row=0, column=6, sticky=tk.W, padx=(0, 4))
    ttk.Entry(f_rep, textvariable=v_scale, width=8).grid(row=0, column=7, padx=(0, 14))

    ttk.Label(f_rep, text="방위각(az):").grid(row=0, column=8, sticky=tk.W, padx=(0, 4))
    ttk.Entry(f_rep, textvariable=v_az, width=6).grid(row=0, column=9, padx=(0, 10))

    ttk.Label(f_rep, text="고도각(el):").grid(row=0, column=10, sticky=tk.W, padx=(0, 4))
    ttk.Entry(f_rep, textvariable=v_el, width=6).grid(row=0, column=11)

    # 4) 실행 버튼 프레임
    f_btns = ttk.Frame(main_frame)
    f_btns.pack(fill=tk.X, pady=(0, 8))

    btn_full = ttk.Button(f_btns, text="▶ [1] Salome 실행 → MED 생성 대기 → 자동 해석 & 리포트")
    btn_full.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4), ipady=6)

    btn_aster = ttk.Button(f_btns, text="▶ [2] 기존 MED 파일로 즉시 해석 & 리포트")
    btn_aster.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=6)

    btn_report = ttk.Button(f_btns, text="▶ [3] 기존 결과(.rmed)로 리포트만 생성")
    btn_report.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=6)

    btn_stop = ttk.Button(f_btns, text="■ 대기 중단", state=tk.DISABLED)
    btn_stop.pack(side=tk.LEFT, padx=(4, 0), ipady=6)

    # 5) 실시간 로그 창
    f_log = ttk.LabelFrame(main_frame, text=" 실행 로그 ", padding=6)
    f_log.pack(fill=tk.BOTH, expand=True)

    txt_log = scrolledtext.ScrolledText(f_log, height=14, font=("Consolas", 9), state=tk.DISABLED)
    txt_log.pack(fill=tk.BOTH, expand=True)

    def append_log(msg: str) -> None:
        def _write():
            txt_log.configure(state=tk.NORMAL)
            txt_log.insert(tk.END, msg + "\n")
            txt_log.see(tk.END)
            txt_log.configure(state=tk.DISABLED)

        root.after(0, _write)

    def set_running_state(running: bool) -> None:
        nonlocal is_running
        is_running = running

        def _update():
            state = tk.DISABLED if running else tk.NORMAL
            btn_full.configure(state=state)
            btn_aster.configure(state=state)
            btn_report.configure(state=state)
            btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)

        root.after(0, _update)

    def run_in_background(mode: str) -> None:
        if is_running:
            return
        try:
            current_cfg = sync_ui_to_cfg()
        except ValueError as e:
            messagebox.showerror("입력값 오류", f"숫자 항목을 올바르게 입력하세요:\n{e}")
            return

        stop_event.clear()
        set_running_state(True)

        def worker():
            pipeline = AutomationPipeline(current_cfg, logger=append_log)
            try:
                pipeline.log("=" * 72)
                if mode == "full":
                    med_path = pipeline.launch_salome_and_wait_for_med(stop_event=stop_event)
                    if not med_path:
                        pipeline.log("파이프라인이 중단되었거나 MED 파일이 감지되지 않았습니다.")
                        return
                    root.after(0, lambda: v_med_file.set(str(med_path)))
                    ok = pipeline.run_aster_analysis()
                    if not ok:
                        show_notification(
                            "Code_Aster 해석 실패",
                            "해석이 정상적으로 완료되지 않았습니다.\n실행 로그 및 analysis.mess를 확인하세요.",
                            is_error=True,
                        )
                        return
                    pdf = pipeline.generate_pdf_report()
                    show_notification(
                        "해석 및 리포트 완료",
                        f"Code_Aster 해석 및 PDF 리포트 생성이 완료되었습니다!\n\n리포트: {pdf}",
                        is_error=False,
                    )

                elif mode == "aster_and_report":
                    ok = pipeline.run_aster_analysis()
                    if not ok:
                        show_notification(
                            "Code_Aster 해석 실패",
                            "해석이 정상적으로 완료되지 않았습니다.\n실행 로그 및 analysis.mess를 확인하세요.",
                            is_error=True,
                        )
                        return
                    pdf = pipeline.generate_pdf_report()
                    show_notification(
                        "해석 및 리포트 완료",
                        f"Code_Aster 해석 및 PDF 리포트 생성이 완료되었습니다!\n\n리포트: {pdf}",
                        is_error=False,
                    )

                elif mode == "report_only":
                    pdf = pipeline.generate_pdf_report()
                    show_notification(
                        "리포트 생성 완료",
                        f"PDF 리포트 생성이 완료되었습니다!\n\n리포트: {pdf}",
                        is_error=False,
                    )

            except Exception as e:
                pipeline.log(f"[예외 발생] {e}")
                show_notification("오류 발생", str(e), is_error=True)
            finally:
                set_running_state(False)

        threading.Thread(target=worker, daemon=True).start()

    btn_full.configure(command=lambda: run_in_background("full"))
    btn_aster.configure(command=lambda: run_in_background("aster_and_report"))
    btn_report.configure(command=lambda: run_in_background("report_only"))
    btn_stop.configure(command=stop_event.set)

    # 초기 MED 파일이 있으면 그룹 목록 자동 로드
    if Path(v_med_file.get()).exists():
        try:
            info = inspect_med_file(v_med_file.get())
            fix_cb["values"] = info["groups"]
            load_cb["values"] = info["groups"]
            append_log(
                f"[초기화 완료] 기존 MED 파일 감지됨: {Path(v_med_file.get()).name} "
                f"(노드 {info['nodes']:,}개, 그룹: {info['groups']})"
            )
        except Exception:
            pass
    append_log("준비가 완료되었습니다. 원하는 실행 모드 버튼을 클릭하세요.")

    root.mainloop()


# ==============================================================================
# 5. CLI 엔트리포인트
# ==============================================================================
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Salome-Meca MED 생성 -> Code_Aster 해석 -> PDF 리포트 통합 프로그램"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="설정 JSON 파일 경로")
    parser.add_argument("--cli", action="store_true", help="GUI 없이 터미널(CLI) 모드로 즉시 실행")
    parser.add_argument("--skip-salome", action="store_true", help="Salome GUI 실행을 건너뛰고 기존 MED 파일로 즉시 해석 및 리포트 진행")
    parser.add_argument("--report-only", action="store_true", help="해석을 건너뛰고 기존 result.rmed로 PDF 리포트만 생성")
    parser.add_argument("--med", default=None, help="해석할 MED 파일 경로 지정")
    parser.add_argument("--rmed", default=None, help="--report-only 시 사용할 RMED 파일 경로 지정")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    if args.med:
        cfg["med_file"] = str(Path(args.med).resolve())

    # 기본 실행은 GUI 대시보드
    if not (args.cli or args.skip_salome or args.report_only):
        run_gui(cfg)
        return 0

    pipeline = AutomationPipeline(cfg)
    try:
        if args.report_only:
            rmed_target = Path(args.rmed).resolve() if args.rmed else None
            pipeline.generate_pdf_report(rmed_target)
            return 0

        if not args.skip_salome:
            med_path = pipeline.launch_salome_and_wait_for_med()
            if not med_path:
                return 1

        ok = pipeline.run_aster_analysis()
        if not ok:
            return 1

        pipeline.generate_pdf_report()
        return 0
    except Exception as e:
        pipeline.log(f"[FATAL] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
