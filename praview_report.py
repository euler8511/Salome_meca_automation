#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rmed_report.py  -  Code_Aster 결과(.rmed / MED) -> PDF 리포트 자동 생성
=====================================================================

리포트 구성
  1. 데이터 정보 / 적용 재질 정보 / Mesh type(요소 타입, 그룹, 품질) 정보
  2. Displacement, von Mises stress 의 Min / Max 값 + 해당 위치가 표시된 컨투어 이미지
  3. 재료 항복강도(Yield strength)를 경계로 색상을 구분한 응력 이미지
     (split : 항복 이하 = 파랑~초록 계열 / 항복 이상 = 노랑~빨강 계열, 경계는 정확히 Yield 등치선)
     (binary: 항복 이하 = 초록 / 항복 이상 = 빨강)

필요 패키지
  pip install numpy h5py matplotlib reportlab

사용 예
  python rmed_report.py result.rmed --sy 250 --E 210 --nu 0.3 --rho 7850 --mat-name "S45C"
  python rmed_report.py result.rmed --comm model.comm --sy 355        # .comm 에서 E, NU, RHO 자동 추출
  python rmed_report.py result.rmed --lang en --scale auto --az -50 --el 20

주의
  * .rmed 파일에는 재질 정보가 저장되지 않습니다(Code_Aster 는 .comm 에만 기록).
    따라서 재질은 CLI 인자(--sy, --E ...) 또는 --comm 파일, 또는 아래 DEFAULT_MATERIAL 로 지정합니다.
  * Code_Aster 는 단위가 없으므로 --length-unit / --stress-unit 으로 모델 단위를 알려주세요.
    (기본값: SI  ->  길이 m, 응력 Pa)
  * 지원 체적 요소: T10 (2차 사면체), TE4 (1차 사면체)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np
from matplotlib.cm import ScalarMappable
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, Normalize
from matplotlib.patches import Patch

# ----------------------------------------------------------------------------
# 사용자 설정 (CLI 인자를 주지 않았을 때의 기본값)
# ----------------------------------------------------------------------------
DEFAULT_MATERIAL = dict(
    name="Structural Steel (ASSUMED)",
    E_GPa=210.0,        # Young's modulus  [GPa]
    nu=0.30,            # Poisson's ratio  [-]
    rho=7850.0,         # Density          [kg/m3]
    yield_MPa=250.0,    # Yield strength   [MPa]
    source="default (assumed) - not stored in .rmed",
)
LENGTH_TO_MM = {"m": 1000.0, "cm": 10.0, "mm": 1.0, "um": 1e-3, "in": 25.4}
STRESS_TO_MPA = {"Pa": 1e-6, "kPa": 1e-3, "MPa": 1.0, "GPa": 1e3}

# MED 요소 코드 -> (이름, 설명, 차원, 절점수)
MED_ELEMENTS = {
    "PO1": ("POINT1", "1-node point", 0, 1),
    "SE2": ("SEG2", "2-node linear segment", 1, 2),
    "SE3": ("SEG3", "3-node quadratic segment", 1, 3),
    "TR3": ("TRIA3", "3-node linear triangle", 2, 3),
    "TR6": ("TRIA6", "6-node quadratic triangle", 2, 6),
    "QU4": ("QUAD4", "4-node linear quadrilateral", 2, 4),
    "QU8": ("QUAD8", "8-node quadratic quadrilateral", 2, 8),
    "QU9": ("QUAD9", "9-node quadratic quadrilateral", 2, 9),
    "TE4": ("TETRA4", "4-node linear tetrahedron", 3, 4),
    "T10": ("TETRA10", "10-node quadratic tetrahedron", 3, 10),
    "PY5": ("PYRAM5", "5-node linear pyramid", 3, 5),
    "P13": ("PYRAM13", "13-node quadratic pyramid", 3, 13),
    "PE6": ("PENTA6", "6-node linear wedge", 3, 6),
    "P15": ("PENTA15", "15-node quadratic wedge", 3, 15),
    "HE8": ("HEXA8", "8-node linear hexahedron", 3, 8),
    "H20": ("HEXA20", "20-node quadratic hexahedron", 3, 20),
}

# T10 / TE4 의 4개 면: (a, b, c, ab, bc, ca, 반대편 절점)  - MED 절점 순서 기준(검증 완료)
TET_FACES = [
    (0, 1, 2, 4, 5, 6, 3),
    (0, 1, 3, 4, 8, 7, 2),
    (1, 2, 3, 5, 9, 8, 0),
    (0, 2, 3, 6, 9, 7, 1),
]

# 색상 정의
CMAP_BELOW = LinearSegmentedColormap.from_list("below", ["#2b4cb3", "#3fa7d6", "#59d2a0", "#a5e05a"])
CMAP_ABOVE = LinearSegmentedColormap.from_list("above", ["#ffe14d", "#ffa31a", "#ee3b23", "#8b0000"])
COL_SAFE, COL_YIELD = "#3fb27f", "#d7301f"
GROUP_PALETTE = ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#17becf"]


# ============================================================================
# 1. MED(.rmed) 읽기
# ============================================================================
def _dec(v) -> str:
    if isinstance(v, (bytes, np.bytes_)):
        return bytes(v).decode("utf-8", "ignore")
    return str(v)


def _split16(s: str):
    return [s[i:i + 16].strip() for i in range(0, len(s), 16) if s[i:i + 16].strip()]


def _parse_descriptor(desc: str) -> dict:
    out = {"generator": "", "os": "", "date": ""}
    parts = [p.strip() for p in desc.split(" - ")]
    if len(parts) >= 2:
        out["generator"] = f"{parts[0]} {parts[1]}".strip()
    elif parts and parts[0]:
        out["generator"] = parts[0]
    if len(parts) >= 4:
        out["os"] = parts[3]
    m = re.search(r"\b[A-Z]{2}-(\d{1,2})-([A-Z]+)-(\d{4})\b", desc)
    if m:
        out["date"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return out


class Model:
    """rmed 에서 읽은 모든 정보를 담는 컨테이너"""


def read_med(path, step_index=-1, disp_field=None, stress_field=None) -> Model:
    path = Path(path)
    M = Model()
    M.path, M.size_bytes = path, path.stat().st_size
    with h5py.File(path, "r") as f:
        desc = _dec(f.attrs.get("descripteur de fichier", b""))
        M.descriptor = _parse_descriptor(desc)
        gi = f["INFOS_GENERALES"].attrs
        M.med_version = f"{int(gi['MAJ'])}.{int(gi['MIN'])}.{int(gi['REL'])}"

        # ---- mesh --------------------------------------------------------
        mesh_names = list(f["ENS_MAA"].keys())
        M.mesh_name = mesh_names[0]
        mg = f["ENS_MAA"][M.mesh_name]
        M.dim = int(mg.attrs["DIM"])
        mstep = mg[sorted(mg.keys())[0]]
        coo = mstep["NOE/COO"]
        n = int(coo.attrs["NBR"])
        space = int(mg.attrs.get("ESP", M.dim))
        X = coo[:].reshape(space, n).T              # MED: no-interlace (x...,y...,z...)
        if space < 3:
            X = np.hstack([X, np.zeros((n, 3 - space))])
        M.X, M.n_nodes = X.astype(float), n

        M.blocks = {}
        for t in mstep["MAI"].keys():
            g = mstep["MAI"][t]
            name, desc_t, dim_t, nn = MED_ELEMENTS.get(t, (t, t, -1, 0))
            if nn == 0:
                nn = g["NOD"].shape[0] // g["FAM"].shape[0]
            ne = g["NOD"].shape[0] // nn
            fam = g["FAM"][:] if "FAM" in g else np.zeros(ne, dtype=np.int32)
            conn = (g["NOD"][:].reshape(nn, ne).T - 1) if dim_t >= 2 else None
            M.blocks[t] = dict(name=name, desc=desc_t, dim=dim_t, nn=nn, n=ne, fam=fam, conn=conn)

        # ---- families / groups ------------------------------------------
        M.fam_groups = {}
        if "FAS" in f and M.mesh_name in f["FAS"]:
            fas = f["FAS"][M.mesh_name]
            for kind in ("ELEME", "NOEUD"):
                if kind not in fas:
                    continue
                for fname in fas[kind].keys():
                    fg = fas[kind][fname]
                    num = int(fg.attrs["NUM"])
                    names = []
                    if "GRO" in fg and "NOM" in fg["GRO"]:
                        for row in np.atleast_2d(fg["GRO/NOM"][:]):
                            s = bytes(row.astype(np.uint8)).decode("utf-8", "ignore").strip("\x00 ").strip()
                            if s:
                                names.append(s)
                    if names:
                        M.fam_groups.setdefault(num, []).extend(names)

        # ---- fields ------------------------------------------------------
        M.fields = []
        for fname in f["CHA"].keys():
            g = f["CHA"][fname]
            steps = sorted(k for k in g.keys())
            locs = set()
            for sk in steps:
                for loc in g[sk].keys():
                    lg = g[sk][loc]
                    if loc == "NOE":
                        locs.add("NODE (NOEU)")
                    elif loc.startswith("NOE."):
                        locs.add("ELNO")
                    elif loc.startswith("MAI.") and _dec(lg.attrs.get("GAU", "")):
                        locs.add("ELGA")
                    else:
                        locs.add("ELEM")
            M.fields.append(dict(name=fname, comps=_split16(_dec(g.attrs["NOM"])), steps=len(steps),
                                 support=", ".join(sorted(locs))))

        names = [d["name"] for d in M.fields]

        def pick(override, test, what):
            if override:
                return override
            for nm in names:
                if test(nm.upper()):
                    return nm
            sys.exit(f"[ERROR] {what} 필드를 찾을 수 없습니다. 존재하는 필드: {names}")

        M.disp_field = pick(disp_field, lambda s: s.endswith("DEPL"), "변위(DEPL)")
        M.stress_field = pick(stress_field, lambda s: "SIEQ" in s and s.endswith("NOEU"), "등가응력(SIEQ_NOEU)")

        def read_nodal(fname, wanted):
            g = f["CHA"][fname]
            comps = _split16(_dec(g.attrs["NOM"]))
            steps = sorted(g.keys())
            sk = steps[step_index]
            ds = g[sk]["NOE"]["MED_NO_PROFILE_INTERNAL"]["CO"]
            if ds.shape[0] != len(comps) * n:
                sys.exit(f"[ERROR] {fname}: 전체 노드에 정의된 필드가 아닙니다(프로파일 사용 추정).")
            out = []
            for c in wanted:                       # no-interlace: 성분별로 연속 저장 -> 필요한 성분만 읽음
                if c in comps:
                    i = comps.index(c)
                    out.append(ds[i * n:(i + 1) * n].astype(float))
                else:
                    out.append(np.zeros(n))
            a = g[sk].attrs
            return np.array(out).T, dict(step_key=sk, NDT=int(a.get("NDT", 0)), NOR=int(a.get("NOR", 0)),
                                         time=float(a.get("PDT", 0.0)), n_steps=len(steps))

        M.U, M.step = read_nodal(M.disp_field, ["DX", "DY", "DZ"])
        v, _ = read_nodal(M.stress_field, ["VMIS"])
        M.vmis = v[:, 0]
    if not (np.isfinite(M.U).all() and np.isfinite(M.vmis).all()):
        sys.exit("[ERROR] 결과 필드에 NaN/Inf 가 있습니다.")
    return M


# ============================================================================
# 2. 재질 (.comm 파싱 지원)
# ============================================================================
def parse_comm(path, stress_to_mpa):
    """DEFI_MATERIAU( ELAS=_F(E=,NU=,RHO=), ECRO_LINE=_F(SY=) ) 에서 값 추출 (숫자 리터럴만 지원)"""
    txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    txt = re.sub(r"#.*", "", txt)
    m = re.search(r"(?:(\w+)\s*=\s*)?DEFI_MATERIAU\s*\(", txt)
    if not m:
        return {}
    i, depth = m.end(), 1
    while i < len(txt) and depth:
        depth += {"(": 1, ")": -1}.get(txt[i], 0)
        i += 1
    body = txt[m.end():i - 1]
    num = r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    out = {}
    if m.group(1):
        out["name"] = m.group(1)
    for key, rx in (("E", rf"\bE\s*=\s*{num}"), ("nu", rf"\bNU\s*=\s*{num}"),
                    ("rho", rf"\bRHO\s*=\s*{num}"), ("sy", rf"\bSY\s*=\s*{num}")):
        mm = re.search(rx, body)
        if mm:
            out[key] = float(mm.group(1))
    res = {}
    if "name" in out:
        res["name"] = out["name"]
    if "E" in out:
        res["E_GPa"] = out["E"] * stress_to_mpa / 1000.0
    if "nu" in out:
        res["nu"] = out["nu"]
    if "rho" in out:
        res["rho"] = out["rho"]
    if "sy" in out:
        res["yield_MPa"] = out["sy"] * stress_to_mpa
    return res


def build_material(args, stress_to_mpa):
    """우선순위: CLI 인자 > .comm 파싱값 > DEFAULT_MATERIAL. 가정값으로 채워진 항목은 assumed_keys 에 기록."""
    mat = dict(DEFAULT_MATERIAL)
    provided, srcs = set(), []
    if args.comm:
        found = parse_comm(args.comm, stress_to_mpa)
        mat.update(found)
        provided |= set(found)
        srcs.append(f".comm ({Path(args.comm).name}): {', '.join(found) or 'nothing parsed'}")
    cli = dict(name=args.mat_name, E_GPa=args.E, nu=args.nu, rho=args.rho, yield_MPa=args.sy)
    given = [k for k, v in cli.items() if v is not None]
    for k in given:
        mat[k] = cli[k]
    provided |= set(given)
    if given:
        srcs.append("CLI: " + ", ".join(given))
    mat["assumed_keys"] = [k for k in ("E_GPa", "nu", "rho", "yield_MPa") if k not in provided]
    if "name" not in provided:
        mat["name"] = DEFAULT_MATERIAL["name"] if not provided else "Unnamed material"
    if mat["assumed_keys"]:
        srcs.append("default (assumed): " + ", ".join(mat["assumed_keys"]))
    mat["source"] = "; ".join(srcs)
    if mat["assumed_keys"]:
        print(f"[WARN] 재질 값 {mat['assumed_keys']} 은(는) 기본 가정값입니다. --sy/--E/--nu/--rho 또는 --comm 으로 지정하세요.")
    return mat


# ============================================================================
# 3. 해석 결과 / 메쉬 통계
# ============================================================================
def analyze(M: Model, mat: dict, L2MM: float, S2MPA: float) -> dict:
    R = {}
    U_mm = M.U * L2MM
    Umag = np.linalg.norm(U_mm, axis=1)
    sig = M.vmis * S2MPA
    R["U_mm"], R["Umag"], R["sig"] = U_mm, Umag, sig

    def mm(arr):
        return dict(min=float(arr.min()), imin=int(arr.argmin()), max=float(arr.max()), imax=int(arr.argmax()))

    R["stat"] = {"|U|": mm(Umag), "DX": mm(U_mm[:, 0]), "DY": mm(U_mm[:, 1]),
                 "DZ": mm(U_mm[:, 2]), "VMIS": mm(sig)}
    sy = mat["yield_MPa"]
    R["sy"] = sy
    R["safety_factor"] = sy / sig.max() if sig.max() > 0 else float("inf")
    R["nodes_above_pct"] = 100.0 * float((sig >= sy).mean())

    # ---- 체적 요소 통계 ----------------------------------------------
    vt = M.vol_type
    conn = M.blocks[vt]["conn"]
    p = M.X[conn[:, :4]]
    vol = np.abs(np.einsum("ij,ij->i", p[:, 1] - p[:, 0], np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 0]))) / 6.0
    pairs = [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]
    el = np.stack([np.linalg.norm(p[:, a] - p[:, b], axis=1) for a, b in pairs], axis=1)
    q = 12.0 * (3.0 * vol) ** (2.0 / 3.0) / np.maximum((el ** 2).sum(1), 1e-300)   # 정사면체 = 1
    R["volume_total"] = float(vol.sum())                       # 모델 길이 단위^3
    R["volume_m3"] = float(vol.sum()) * (L2MM / 1000.0) ** 3
    R["edge"] = dict(min=float(el.min()) * L2MM, mean=float(el.mean()) * L2MM, max=float(el.max()) * L2MM)
    R["quality"] = dict(min=float(q.min()), mean=float(q.mean()), max=float(q.max()))
    sig_el = sig[conn].mean(axis=1)
    R["elems_above_pct"] = 100.0 * float((sig_el >= sy).mean())
    R["volume_above_pct"] = 100.0 * float(vol[sig_el >= sy].sum() / vol.sum())
    R["bbox"] = (M.X.min(0), M.X.max(0))
    return R


# ============================================================================
# 4. 표면 추출 / 투영 / 렌더링
# ============================================================================
def _key3(tri_sorted, n):
    t = tri_sorted.astype(np.int64)
    return (t[:, 0] * n + t[:, 1]) * n + t[:, 2]


def extract_boundary(X, conn, quadratic):
    """T10/TE4 체적 요소의 외곽 면(경계면)을 추출. 외향 법선이 되도록 방향 정렬.
    반환: (nb,6) [a,b,c,ab,bc,ca] (선형이면 mid=-1)"""
    n, ne = len(X), len(conn)
    rows = []
    opp = []
    for (a, b, c, ab, bc, ca, o) in TET_FACES:
        mids = conn[:, [ab, bc, ca]] if quadratic else -np.ones((ne, 3), dtype=conn.dtype)
        rows.append(np.hstack([conn[:, [a, b, c]], mids]))
        opp.append(conn[:, o])
    F, O = np.vstack(rows), np.concatenate(opp)
    key = _key3(np.sort(F[:, :3], axis=1), n)
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    bmask = cnt[inv] == 1
    F, O = F[bmask], O[bmask]
    # 방향: 법선이 반대편 절점에서 멀어지도록
    pa, pb, pc = X[F[:, 0]], X[F[:, 1]], X[F[:, 2]]
    nrm = np.cross(pb - pa, pc - pa)
    flip = np.einsum("ij,ij->i", nrm, X[O] - pa) > 0
    Ff = F.copy()
    Ff[flip] = F[flip][:, [0, 2, 1, 5, 4, 3]]       # b<->c 교환 시 ab<->ca
    return Ff


def subdivide(F, quadratic):
    if not quadratic:
        return F[:, :3].copy()
    a, b, c, ab, bc, ca = F.T
    return np.vstack([np.stack(t, 1) for t in ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))])


def clip_at_level(T, V, L):
    """삼각형을 등치선 V=L 에서 분할. 반환: T, V, side(0: L 미만, 1: L 이상)"""
    above = V >= L
    na = above.sum(1)
    uni = (na == 0) | (na == 3)
    outT, outV, outS = [T[uni]], [V[uni]], [(na[uni] == 3).astype(np.int8)]
    mix = ~uni
    if mix.any():
        Tm, Vm, am, nam = T[mix], V[mix], above[mix], na[mix]
        lone_above = nam == 1
        i0 = np.where(lone_above, am.argmax(1), (~am).argmax(1))
        ar = np.arange(len(Tm))
        i1, i2 = (i0 + 1) % 3, (i0 + 2) % 3
        P0, P1, P2 = Tm[ar, i0], Tm[ar, i1], Tm[ar, i2]
        v0, v1, v2 = Vm[ar, i0], Vm[ar, i1], Vm[ar, i2]
        t1, t2 = (L - v0) / (v1 - v0), (L - v0) / (v2 - v0)
        A = P0 + t1[:, None] * (P1 - P0)
        B = P0 + t2[:, None] * (P2 - P0)
        Lc = np.full_like(v0, L)
        s_lone, s_rest = lone_above.astype(np.int8), (~lone_above).astype(np.int8)
        outT += [np.stack([P0, A, B], 1), np.stack([A, P1, P2], 1), np.stack([A, P2, B], 1)]
        outV += [np.stack([v0, Lc, Lc], 1), np.stack([Lc, v1, v2], 1), np.stack([Lc, v2, Lc], 1)]
        outS += [s_lone, s_rest, s_rest]
    return np.concatenate(outT), np.concatenate(outV), np.concatenate(outS)


class Scene:
    """정사영 카메라 + 경계면 메쉬"""

    def __init__(self, X0, F, quadratic, az, el, face_fam=None, fam_names=None):
        self.X0, self.F, self.quadratic = X0, F, quadratic
        self.tris = subdivide(F, quadratic)
        self.face_fam, self.fam_names = face_fam, fam_names or {}
        a, e = math.radians(az), math.radians(el)
        self.c = np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
        self.r = np.array([-math.sin(a), math.cos(a), 0.0])
        self.u = np.array([-math.sin(e) * math.cos(a), -math.sin(e) * math.sin(a), math.cos(e)])
        # 모서리 위상(feature edge 용)
        corner, nn = F[:, :3], len(X0)
        e3 = np.concatenate([corner[:, [0, 1]], corner[:, [1, 2]], corner[:, [2, 0]]])
        tid = np.tile(np.arange(len(corner)), 3)
        es = np.sort(e3, 1)
        key = es[:, 0].astype(np.int64) * nn + es[:, 1]
        order = np.argsort(key, kind="stable")
        ks = key[order]
        same = ks[1:] == ks[:-1]
        i1, i2 = order[:-1][same], order[1:][same]
        self.e_nodes, self.e_t1, self.e_t2 = es[i1], tid[i1], tid[i2]

    def proj(self, P):
        return np.stack([P @ self.r, P @ self.u, P @ self.c], axis=-1)

    def face_normals(self, P):
        c = P[self.F[:, :3]]
        return np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])

    def feature_segments(self, P, angle_deg=35.0):
        n = self.face_normals(P)
        front = n @ self.c > 0
        nn = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-300)
        dot = np.einsum("ij,ij->i", nn[self.e_t1], nn[self.e_t2])
        sel = (dot <= math.cos(math.radians(angle_deg))) & (front[self.e_t1] | front[self.e_t2])
        return self.proj(P[self.e_nodes[sel]])[..., :2]


def _fmt(v):
    a = abs(v)
    if a != 0 and (a >= 1e6 or a < 1e-3):
        return f"{v:.3e}"
    return f"{v:,.3f}" if a < 1000 else f"{v:,.1f}"


def _tick(v):
    return f"{v:.4g}"


def _canvas(scene, P_list, header_title, header_lines, bottom_in, W=11.0, ax_w=10.4):
    """제목/정보 헤더 + 정사영 축 + 하단(컬러바/범례) 영역을 가진 Figure 생성"""
    pts = np.vstack([scene.proj(P)[:, :2] for P in P_list])
    (x0, y0), (x1, y1) = pts.min(0), pts.max(0)
    Wd, Hd = max(x1 - x0, 1e-12), max(y1 - y0, 1e-12)
    padx, pady = 0.06 * Wd, 0.12 * Hd + 0.035 * Wd
    xlim, ylim = (x0 - padx, x1 + padx), (y0 - pady, y1 + pady)
    ax_h = float(np.clip(ax_w * (ylim[1] - ylim[0]) / (xlim[1] - xlim[0]), 2.4, 6.0))
    head_in = 0.55 + 0.25 * len(header_lines)
    H = head_in + ax_h + bottom_in
    fig = plt.figure(figsize=(W, H), dpi=100, facecolor="white")
    ax = fig.add_axes([(W - ax_w) / 2 / W, bottom_in / H, ax_w / W, ax_h / H])
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.set_aspect("equal", adjustable="box"); ax.axis("off")
    fig.text(0.02, 1 - 0.12 / H, header_title, fontsize=15, fontweight="bold", va="top", ha="left")
    for i, (txt, col) in enumerate(header_lines):
        fig.text(0.02, 1 - (0.55 + 0.25 * i) / H, txt, fontsize=10.5, va="top", ha="left", color=col,
                 family="DejaVu Sans Mono")
    # 좌표축 triad
    tr = fig.add_axes([0.012, (bottom_in + 0.05) / H, 0.85 / W, 0.85 / H])
    tr.set_xlim(-1.5, 1.5); tr.set_ylim(-1.5, 1.5); tr.set_aspect("equal"); tr.axis("off")
    for i, (lab, col) in enumerate(zip("XYZ", ("#d62728", "#2ca02c", "#1f77b4"))):
        e = np.zeros(3); e[i] = 1
        dx, dy = float(e @ scene.r), float(e @ scene.u)
        tr.annotate("", xy=(dx, dy), xytext=(0, 0), arrowprops=dict(arrowstyle="-|>", color=col, lw=1.5))
        tr.text(dx * 1.3, dy * 1.3, lab, color=col, ha="center", va="center", fontsize=9, fontweight="bold")
    return fig, ax, (W, H, bottom_in), (xlim, ylim)


def _marks(ax, scene, P, marks, vis, xlim, ylim):
    for m in marks:
        xy = scene.proj(P[m["node"]])[:2]
        hidden = not vis[m["node"]]
        ax.plot(*xy, marker=m["marker"], ms=10, mfc="none" if hidden else m["color"],
                mec=m["color"] if hidden else "k", mew=1.4, zorder=20, linestyle="none")
        fx = (xy[0] - xlim[0]) / (xlim[1] - xlim[0])
        fy = (xy[1] - ylim[0]) / (ylim[1] - ylim[0])
        ha = "right" if fx > 0.7 else ("left" if fx < 0.3 else "center")
        dx = -16 if ha == "right" else (16 if ha == "left" else 0)
        if m["kind"] == "max":
            dy = 34 if fy < 0.6 else -40
        else:
            dy = -40 if fy > 0.4 else 34
        txt = m["text"] + ("\n(on far side)" if hidden else "")
        ax.annotate(txt, xy=xy, xytext=(dx, dy), textcoords="offset points", ha=ha, va="center", fontsize=9,
                    fontweight="bold", color=m["color"], zorder=25,
                    bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=m["color"], alpha=0.92),
                    arrowprops=dict(arrowstyle="-", color=m["color"], lw=1.0))


def render_scalar(scene, P, values, out_png, *, mode, title, header_lines, vmin, vmax, label,
                  sy=None, marks=(), ghost=True, cmap_name="turbo", binary_labels=None):
    """mode: 'continuous' | 'split' (항복 경계 2색 계열) | 'binary' (항복 이하/이상 2색)"""
    T3 = P[scene.tris]
    nrm = np.cross(T3[:, 1] - T3[:, 0], T3[:, 2] - T3[:, 0])
    front = nrm @ scene.c > 0
    tn, T = scene.tris[front], T3[front]
    V = values[tn]
    vis = np.zeros(len(P), bool)
    vis[tn.ravel()] = True

    side = None
    if mode in ("split", "binary"):
        T, V, side = clip_at_level(T, V, sy)

    xyz = scene.proj(T)
    order = np.argsort(xyz[..., 2].mean(1))                    # painter's algorithm (먼 것부터)
    xy, V = xyz[order][..., :2], V[order]
    if side is not None:
        side = side[order]

    if mode == "continuous":
        u = np.clip((V - vmin) / max(vmax - vmin, 1e-12), 0, 1)
        cmap = plt.get_cmap(cmap_name)
    elif mode == "split":
        ub = np.clip((V - vmin) / max(sy - vmin, 1e-12), 0, 1) * 0.4999
        ua = 0.5 + np.clip((V - sy) / max(vmax - sy, 1e-12), 0, 1) * 0.5
        u = np.where((side == 0)[:, None], ub, ua)
        cmap = ListedColormap(np.vstack([CMAP_BELOW(np.linspace(0, 1, 512)), CMAP_ABOVE(np.linspace(0, 1, 512))]))
    else:
        u = np.where((side == 0)[:, None], 0.25, 0.75) * np.ones_like(V)
        cmap = ListedColormap([COL_SAFE, COL_YIELD])

    bottom_in = 1.25 if mode != "binary" else 0.8
    plist = [P] + ([scene.X0] if ghost else [])
    fig, ax, (W, H, bin_), (xlim, ylim) = _canvas(scene, plist, title, header_lines, bottom_in)

    N = len(xy)
    tri = mtri.Triangulation(xy[..., 0].ravel(), xy[..., 1].ravel(), np.arange(3 * N).reshape(N, 3))
    ax.tripcolor(tri, u.ravel(), shading="gouraud", cmap=cmap, vmin=0.0, vmax=1.0, zorder=2)

    if ghost and P is not scene.X0:
        g = scene.feature_segments(scene.X0)
        ax.add_collection(LineCollection(g, colors="#9a9a9a", linewidths=0.7, linestyles=(0, (4, 3)), zorder=1))
    ax.add_collection(LineCollection(scene.feature_segments(P), colors="#1b1b1b", linewidths=0.8, zorder=5))
    _marks(ax, scene, P, marks, vis, xlim, ylim)

    # ---- 컬러바 / 범례 ---------------------------------------------------
    cax = fig.add_axes([0.2, (bin_ - 0.72) / H, 0.6, 0.22 / H])
    if mode == "continuous":
        cb = fig.colorbar(ScalarMappable(Normalize(vmin, vmax), cmap), cax=cax, orientation="horizontal")
        cb.set_label(label, fontsize=10)
        cb.ax.tick_params(labelsize=9)
    elif mode == "split":
        cax.imshow(np.linspace(0, 1, 1024)[None, :], aspect="auto", cmap=cmap, vmin=0, vmax=1, extent=[0, 1, 0, 1])
        pos = [0, 0.25, 0.5, 0.75, 1]
        vals = [vmin, (vmin + sy) / 2, sy, (sy + vmax) / 2, vmax]
        cax.set_xticks(pos)
        cax.set_xticklabels([_tick(v) for v in vals], fontsize=9)
        cax.get_xticklabels()[2].set_fontweight("bold")
        cax.set_yticks([])
        cax.axvline(0.5, color="k", lw=2.0)
        cax.set_xlabel(label + "   [split at yield strength]", fontsize=10)
        cax.text(0.5, 1.25, rf"$\sigma_y$ = {_tick(sy)}", transform=cax.transAxes, ha="center", va="bottom",
                 fontsize=10, fontweight="bold")
        cax.text(0.25, 0.5, "below yield", transform=cax.transAxes, ha="center", va="center", fontsize=9,
                 color="white", fontweight="bold")
        cax.text(0.75, 0.5, "above yield", transform=cax.transAxes, ha="center", va="center", fontsize=9,
                 color="white", fontweight="bold")
    else:
        cax.axis("off")
        lab_lo, lab_hi = binary_labels
        fig.legend(handles=[Patch(fc=COL_SAFE, ec="k", label=lab_lo), Patch(fc=COL_YIELD, ec="k", label=lab_hi)],
                   loc="lower center", bbox_to_anchor=(0.5, 0.12 / H), ncol=2, fontsize=11, frameon=True)
    fig.savefig(out_png, dpi=200)
    plt.close(fig)


def render_mesh(scene, out_png, title, header_lines, face_fam, fam_names, ungrouped_label):
    P = scene.X0
    F = scene.F
    c = P[F[:, :3]]
    nrm = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])
    front = nrm @ scene.c > 0
    idx = np.where(front)[0]
    cols_poly = F[:, [0, 3, 1, 4, 2, 5]] if scene.quadratic else F[:, :3]
    polys = scene.proj(P[cols_poly[idx]])
    order = np.argsort(polys[..., 2].mean(1))
    idx, polys = idx[order], polys[order][..., :2]

    fams = sorted({int(x) for x in np.unique(face_fam) if x != 0 and x in fam_names})
    cmap = {fm: GROUP_PALETTE[i % len(GROUP_PALETTE)] for i, fm in enumerate(fams)}
    fc = [cmap.get(int(face_fam[i]), "#d9dde3") for i in idx]

    fig, ax, (W, H, bin_), (xlim, ylim) = _canvas(scene, [P], title, header_lines, 0.8)
    ax.add_collection(PolyCollection(polys, facecolors=fc, edgecolors="#2b2b2b", linewidths=0.12, zorder=2))
    ax.add_collection(LineCollection(scene.feature_segments(P), colors="#000000", linewidths=0.9, zorder=5))
    handles = [Patch(fc=cmap[fm], ec="k", label=", ".join(fam_names[fm]) + " (surface group)") for fm in fams]
    handles.append(Patch(fc="#d9dde3", ec="k", label=ungrouped_label))
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.1 / H), ncol=min(len(handles), 4),
               fontsize=10, frameon=True)
    fig.savefig(out_png, dpi=200)
    plt.close(fig)


def render_hist(sig, sy, out_png, label):
    fig, ax = plt.subplots(figsize=(9, 3.4), dpi=100)
    bins = np.linspace(sig.min(), sig.max(), 90)
    ax.hist(sig[sig < sy], bins=bins, color=COL_SAFE, label=f"below yield ({(sig < sy).mean() * 100:.1f}% of nodes)")
    ax.hist(sig[sig >= sy], bins=bins, color=COL_YIELD, label=f"at/above yield ({(sig >= sy).mean() * 100:.1f}% of nodes)")
    ax.axvline(sy, color="k", ls="--", lw=1.5)
    ax.text(sy, ax.get_ylim()[1] * 0.97, rf"  $\sigma_y$ = {_tick(sy)} MPa", ha="left", va="top", fontweight="bold")
    ax.set_xlabel(label)
    ax.set_ylabel("Node count")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper right", bbox_to_anchor=(1.0, 0.85))
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)


# ============================================================================
# 5. PDF
# ============================================================================
TXT = {
    "ko": dict(
        title="구조해석 결과 리포트", sub="Code_Aster 결과 파일(.rmed) 기반 자동 생성",
        generated="리포트 생성일시",
        s1="1. 데이터 정보", s2="2. 적용 재질 정보", s3="3. Mesh 정보", s4="4. 해석 결과 (Min / Max)",
        s5="5. 항복강도(Yield strength) 기준 평가", s6="6. 비고",
        file="결과 파일", size="파일 크기", medv="MED 포맷 버전", gen="생성 프로그램", wdate="파일 기록일(헤더)",
        os="운영체제", meshname="Mesh 이름", sdim="공간 차원", nnodes="전체 노드 수", bbox="모델 범위 (Bounding box)",
        stepinfo="사용한 결과 스텝", analysis="해석 유형", units="모델 단위(가정)",
        single="단일 결과 스텝 (정적 해석으로 판단)", multi="다중 스텝",
        fields="결과 필드 목록", c_field="필드명", c_comp="성분", c_sup="지원 위치", c_steps="스텝 수", c_used="사용",
        used_d="변위(Displacement)", used_s="등가응력(von Mises)",
        mname="재질명", mE="탄성계수 (Young's modulus, E)", mnu="포아송비 (Poisson's ratio)", mrho="밀도 (Density)", msy="항복강도 (Yield strength)", msrc="입력 출처",
        mwarn="※ 재질 정보는 .rmed 파일에 저장되어 있지 않습니다. 다음 항목은 기본 가정값이 사용되었으니 실제 값으로 "
              "--sy, --E, --nu, --rho 또는 --comm 옵션을 지정하세요: {keys}",
        assumed="가정값",
        et="요소 타입 (Mesh type)", c_type="MED 코드", c_name="요소명", c_desc="설명", c_dim="차원", c_nn="절점/요소",
        c_cnt="요소 수", c_role="역할",
        r3="체적(Solid) 요소", r2="표면(경계면) 요소", r1="선(Edge) 요소", r0="점 요소",
        grp="그룹 (Group)", c_grp="그룹명", c_gtype="요소 타입", gnote="그룹에 속하지 않은 요소는 표에서 제외됨",
        mstat="Mesh 통계", mvol="모델 체적", medge="요소 모서리 길이 (최소 / 평균 / 최대)",
        mq="요소 품질 (shape ratio, 1 = 정사면체)", mq_v="최소 / 평균 / 최대",
        q="항목", mn="최소", mx="최대", node="노드",
        disp_mag="변위 크기 |U|", dx="변위 DX", dy="변위 DY", dz="변위 DZ", vm="von Mises 응력",
        sf="안전율 (항복강도 / 최대 von Mises)", na="항복 초과 노드 비율", ea="항복 초과 요소 비율 (개수 기준)",
        va="항복 초과 체적 비율",
        f_mesh="Mesh 및 경계면 그룹", f_disp="변위 크기 컨투어 (Min/Max 표시)",
        f_vm="von Mises 응력 컨투어 (Min/Max 표시)",
        f_split="항복강도 경계 색상 구분 (Split scale)", f_bin="항복 이하 / 이상 구분 (Binary)",
        f_hist="von Mises 응력 분포 (노드 기준)",
        n1="변위는 mm, 응력은 MPa 로 환산하여 표기하였습니다. 모델 단위는 실행 인자(--length-unit, --stress-unit)를 따릅니다.",
        n2="von Mises 응력은 SIEQ_NOEU(노드 평균) 필드 값입니다. 요소별 값은 노드값의 평균으로 계산하였습니다.",
        n3="체적은 요소 꼭짓점 4개 기준으로 계산한 근사값입니다. 변형 형상은 실제 변위의 {scale} 배로 표시되었습니다.",
        n4="Fixed 경계 모서리 등 응력 특이점에서는 메쉬 크기에 따라 최대 응력이 과대 평가될 수 있습니다.",
        opp="반대 방향 시점", page="페이지",
        below="항복 이하", above="항복 이상",
    ),
    "en": dict(
        title="Structural Analysis Report", sub="Auto-generated from Code_Aster result file (.rmed)",
        generated="Generated",
        s1="1. Data Information", s2="2. Material Information", s3="3. Mesh Information",
        s4="4. Results (Min / Max)", s5="5. Yield Strength Assessment", s6="6. Notes",
        file="Result file", size="File size", medv="MED format version", gen="Generator", wdate="Written (file header)",
        os="Operating system", meshname="Mesh name", sdim="Space dimension", nnodes="Number of nodes",
        bbox="Model extents (bounding box)", stepinfo="Result step used", analysis="Analysis type",
        units="Model units (assumed)", single="Single result step (static analysis assumed)", multi="Multiple steps",
        fields="Result fields", c_field="Field", c_comp="Components", c_sup="Support", c_steps="Steps", c_used="Used for",
        used_d="Displacement", used_s="von Mises stress",
        mname="Material name", mE="Young's modulus E", mnu="Poisson's ratio", mrho="Density", msy="Yield strength",
        msrc="Source",
        mwarn="NOTE: Material data is not stored in the .rmed file. Assumed default values were used for: {keys}. "
              "Specify real values with --sy, --E, --nu, --rho or --comm.",
        assumed="assumed",
        et="Element types (Mesh type)", c_type="MED code", c_name="Name", c_desc="Description", c_dim="Dim",
        c_nn="Nodes/elem", c_cnt="Count", c_role="Role",
        r3="Solid (volume) elements", r2="Surface (boundary) elements", r1="Line (edge) elements", r0="Point elements",
        grp="Groups", c_grp="Group", c_gtype="Element type", gnote="Elements not belonging to any group are not listed",
        mstat="Mesh statistics", mvol="Model volume", medge="Element edge length (min / mean / max)",
        mq="Element quality (shape ratio, 1 = regular tet)", mq_v="min / mean / max",
        q="Quantity", mn="Min", mx="Max", node="Node",
        disp_mag="Displacement magnitude |U|", dx="Displacement DX", dy="Displacement DY", dz="Displacement DZ",
        vm="von Mises stress",
        sf="Safety factor (yield / max von Mises)", na="Nodes at/above yield", ea="Elements at/above yield (count)",
        va="Volume at/above yield",
        f_mesh="Mesh and boundary groups", f_disp="Displacement magnitude contour (Min/Max marked)",
        f_vm="von Mises stress contour (Min/Max marked)", f_split="Yield-split color scale",
        f_bin="Below / above yield (binary)", f_hist="von Mises stress distribution (nodes)",
        n1="Displacements are shown in mm and stresses in MPa (converted from the model units given by --length-unit / --stress-unit).",
        n2="von Mises stress is taken from the SIEQ_NOEU (nodal-averaged) field. Element values are means of nodal values.",
        n3="Volumes are approximated from the 4 corner nodes. The deformed shape is drawn at {scale}x the real displacement.",
        n4="At stress singularities (e.g. fixed-support edges) the peak stress depends strongly on mesh size.",
        opp="opposite view", page="Page",
        below="Below yield (< sigma_y)", above="At/above yield (>= sigma_y)",
    ),
}


def setup_fonts(lang, font_path=None):
    from reportlab.lib.fonts import addMapping  # noqa: F401
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont

    if lang == "en" and not font_path:
        return "en", "Helvetica", "Helvetica-Bold"
    here = Path(__file__).resolve().parent
    pairs = []
    if font_path:
        pairs.append((font_path, font_path))
    pairs += [
        (r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf"),
        (str(here / "fonts" / "NanumGothic.ttf"), str(here / "fonts" / "NanumGothicBold.ttf")),
        ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
        ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", "/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
    ]
    for reg, bold in pairs:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont("RptFont", reg))
                pdfmetrics.registerFont(TTFont("RptFont-Bold", bold if os.path.exists(bold) else reg))
                registerFontFamily("RptFont", normal="RptFont", bold="RptFont-Bold", italic="RptFont",
                                   boldItalic="RptFont-Bold")
                return lang, "RptFont", "RptFont-Bold"
            except Exception as ex:  # 폰트 형식 문제(CFF 등) -> 다음 후보
                print(f"[WARN] 폰트 로드 실패 {reg}: {ex}")
    print("[WARN] 한글 TrueType 폰트를 찾지 못해 영어 리포트로 생성합니다. (--font 로 .ttf 경로 지정 가능)")
    return "en", "Helvetica", "Helvetica-Bold"


def build_pdf(out_pdf, M, mat, R, figs, args, lang, fnt_reg, fnt_bold, scale_used):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    tx = TXT[lang]
    st = dict(
        title=ParagraphStyle("t", fontName=fnt_bold, fontSize=22, leading=28, spaceAfter=2),
        sub=ParagraphStyle("s", fontName=fnt_reg, fontSize=10, leading=14, textColor=colors.HexColor("#555555")),
        h1=ParagraphStyle("h1", fontName=fnt_bold, fontSize=13.5, leading=18, spaceBefore=10, spaceAfter=5,
                          textColor=colors.HexColor("#1f3a5f"), keepWithNext=1),
        h2=ParagraphStyle("h2", fontName=fnt_bold, fontSize=10.5, leading=14, spaceBefore=7, spaceAfter=3,
                          keepWithNext=1),
        body=ParagraphStyle("b", fontName=fnt_reg, fontSize=9, leading=12.5),
        cell=ParagraphStyle("c", fontName=fnt_reg, fontSize=8.3, leading=10.8),
        cellb=ParagraphStyle("cb", fontName=fnt_bold, fontSize=8.3, leading=10.8),
        head=ParagraphStyle("hd", fontName=fnt_bold, fontSize=8.3, leading=10.8, textColor=colors.white),
        cap=ParagraphStyle("cp", fontName=fnt_reg, fontSize=8.3, leading=11, alignment=TA_CENTER,
                           textColor=colors.HexColor("#444444"), spaceAfter=8),
        warn=ParagraphStyle("w", fontName=fnt_bold, fontSize=8.5, leading=11.5, textColor=colors.HexColor("#b02a1c")),
    )
    CW = 174 * mm

    def P(s, style="cell"):
        return Paragraph(s, st[style])

    def E(s):
        return escape(str(s))

    def kv(rows, w1=0.30):
        data = [[P(k, "cellb"), P(v)] for k, v in rows]
        t = Table(data, colWidths=[CW * w1, CW * (1 - w1)])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5ccd6")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f7")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
        return t

    def grid(header, rows, widths):
        data = [[P(h, "head") for h in header]] + [[P(c) if not hasattr(c, "wrap") else c for c in r] for r in rows]
        t = Table(data, colWidths=[CW * w for w in widths], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5ccd6")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fb")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
        return t

    def img(path, caption, width=CW):
        iw, ih = ImageReader(str(path)).getSize()
        return KeepTogether([Image(str(path), width=width, height=width * ih / iw), P(caption, "cap")])

    L, S = args.length_unit, args.stress_unit
    lo, hi = R["bbox"]
    sx = hi - lo
    story = [P(tx["title"], "title"), P(tx["sub"], "sub"),
             P(f"{tx['generated']}: {dt.datetime.now():%Y-%m-%d %H:%M}", "sub"), Spacer(1, 6)]

    # ---------------- 1. 데이터 정보 ----------------
    story.append(P(tx["s1"], "h1"))
    stp = M.step
    step_txt = (f"#{stp['NOR']}  (NDT={stp['NDT']}, t = {stp['time']:g})  /  total {stp['n_steps']}")
    rows = [
        (tx["file"], E(M.path.name)),
        (tx["size"], f"{M.size_bytes / 1024 ** 2:,.1f} MB"),
        (tx["medv"], E(M.med_version)),
        (tx["gen"], E(M.descriptor["generator"] or "-")),
        (tx["wdate"], E(M.descriptor["date"] or "-")),
        (tx["os"], E(M.descriptor["os"] or "-")),
        (tx["meshname"], E(M.mesh_name)),
        (tx["sdim"], f"{M.dim}D"),
        (tx["nnodes"], f"{M.n_nodes:,}"),
        (tx["bbox"], f"X: {lo[0]:g} ~ {hi[0]:g} &nbsp; Y: {lo[1]:g} ~ {hi[1]:g} &nbsp; Z: {lo[2]:g} ~ {hi[2]:g} "
                     f"[{L}] &nbsp; (size {sx[0]:g} x {sx[1]:g} x {sx[2]:g})"),
        (tx["stepinfo"], step_txt),
        (tx["analysis"], tx["single"] if stp["n_steps"] == 1 else tx["multi"]),
        (tx["units"], f"{'length'}: {L}, {'stress'}: {S}"),
    ]
    story += [kv(rows), Spacer(1, 6), P(tx["fields"], "h2")]
    frows = []
    for d in M.fields:
        used = tx["used_d"] if d["name"] == M.disp_field else (tx["used_s"] if d["name"] == M.stress_field else "")
        comps = ", ".join(d["comps"]) if len(d["comps"]) <= 6 else ", ".join(d["comps"][:5]) + f", ... ({len(d['comps'])})"
        frows.append([E(d["name"]), E(comps), E(d["support"]), str(d["steps"]), E(used)])
    story.append(grid([tx["c_field"], tx["c_comp"], tx["c_sup"], tx["c_steps"], tx["c_used"]], frows,
                      [0.25, 0.30, 0.15, 0.08, 0.22]))

    # ---------------- 2. 재질 ----------------
    story.append(P(tx["s2"], "h1"))
    ak = mat["assumed_keys"]

    def tag(k):
        return f' <font color="#b02a1c">({tx["assumed"]})</font>' if k in ak else ""

    mrows = [
        (tx["mname"], E(mat["name"])),
        (tx["mE"], f"{mat['E_GPa']:g} GPa" + tag("E_GPa")),
        (tx["mnu"], f"{mat['nu']:g}" + tag("nu")),
        (tx["mrho"], f"{mat['rho']:g} kg/m<super>3</super>" + tag("rho")),
        (tx["msy"], f"<b>{mat['yield_MPa']:g} MPa</b>" + tag("yield_MPa")),
        (tx["msrc"], E(mat["source"])),
    ]
    story.append(kv(mrows))
    if ak:
        story += [Spacer(1, 3), P(tx["mwarn"].format(keys=", ".join(ak)), "warn")]

    # ---------------- 3. Mesh ----------------
    story.append(P(tx["s3"], "h1"))
    role = {3: tx["r3"], 2: tx["r2"], 1: tx["r1"], 0: tx["r0"]}
    erows = []
    for t, b in sorted(M.blocks.items(), key=lambda kv_: (-kv_[1]["dim"], kv_[0])):
        erows.append([E(t), E(b["name"]), E(b["desc"]), str(b["dim"]), str(b["nn"]), f"{b['n']:,}",
                      E(role.get(b["dim"], "-"))])
    story += [P(tx["et"], "h2"),
              grid([tx["c_type"], tx["c_name"], tx["c_desc"], tx["c_dim"], tx["c_nn"], tx["c_cnt"], tx["c_role"]],
                   erows, [0.09, 0.12, 0.26, 0.06, 0.09, 0.11, 0.27])]
    grows = []
    for num, names in sorted(M.fam_groups.items()):
        for t, b in M.blocks.items():
            cnt = int((b["fam"] == num).sum())
            if cnt:
                for nm in names:
                    grows.append([E(nm), E(f"{t} ({b['name']})"), f"{cnt:,}"])
    if grows:
        story += [Spacer(1, 4), P(tx["grp"], "h2"), grid([tx["c_grp"], tx["c_gtype"], tx["c_cnt"]], grows, [0.4, 0.4, 0.2]),
                  P(tx["gnote"], "sub")]
    e, q = R["edge"], R["quality"]
    story += [Spacer(1, 4), P(tx["mstat"], "h2"), kv([
        (tx["mvol"], f"{R['volume_total']:.6g} {L}<super>3</super>"),
        (tx["medge"], f"{e['min']:.3f} / {e['mean']:.3f} / {e['max']:.3f} mm"),
        (tx["mq"], f"{q['min']:.3f} / {q['mean']:.3f} / {q['max']:.3f}  ({tx['mq_v']})"),
    ], 0.45), Spacer(1, 6), img(figs["mesh"], f"Fig. 1-a  {tx['f_mesh']}", width=CW * 0.85),
              img(figs["mesh_b"], f"Fig. 1-b  {tx['f_mesh']} ({tx['opp']})", width=CW * 0.85)]

    # ---------------- 4. 결과 ----------------
    story.append(P(tx["s4"], "h1"))
    X = M.X

    def nodetxt(i):
        return f"N{i + 1}<br/>({X[i, 0]:.4g}, {X[i, 1]:.4g}, {X[i, 2]:.4g})"

    rr = []
    for key, lab, unit in (("|U|", tx["disp_mag"], "mm"), ("DX", tx["dx"], "mm"), ("DY", tx["dy"], "mm"),
                           ("DZ", tx["dz"], "mm"), ("VMIS", tx["vm"], "MPa")):
        s = R["stat"][key]
        bold = key in ("|U|", "VMIS")
        f = (lambda v: f"<b>{_fmt(v)}</b>") if bold else _fmt
        rr.append([f"{'<b>' if bold else ''}{lab}{'</b>' if bold else ''} [{unit}]", f(s["min"]), nodetxt(s["imin"]),
                   f(s["max"]), nodetxt(s["imax"])])
    story += [grid([tx["q"], tx["mn"], f"{tx['mn']} {tx['node']} (x, y, z) [{L}]", tx["mx"],
                    f"{tx['mx']} {tx['node']} (x, y, z) [{L}]"], rr, [0.22, 0.13, 0.25, 0.13, 0.27]), Spacer(1, 8),
              img(figs["disp"], f"Fig. 2  {tx['f_disp']}"), img(figs["vm"], f"Fig. 3  {tx['f_vm']}")]

    # ---------------- 5. Yield ----------------
    story.append(P(tx["s5"], "h1"))
    story.append(kv([
        (tx["msy"], f"<b>{R['sy']:g} MPa</b> &nbsp; ({E(mat['name'])})"),
        (tx["vm"] + " max", f"{_fmt(R['stat']['VMIS']['max'])} MPa"),
        (tx["sf"], f"<b>{R['safety_factor']:.3f}</b>" + ("  &nbsp; (&lt; 1.0)" if R["safety_factor"] < 1 else "")),
        (tx["na"], f"{R['nodes_above_pct']:.2f} %"),
        (tx["ea"], f"{R['elems_above_pct']:.2f} %"),
        (tx["va"], f"{R['volume_above_pct']:.2f} %"),
    ], 0.45))
    story += [Spacer(1, 8), img(figs["split"], f"Fig. 4  {tx['f_split']}"), img(figs["binary"], f"Fig. 5  {tx['f_bin']}"),
              img(figs["hist"], f"Fig. 6  {tx['f_hist']}", width=CW * 0.85)]

    # ---------------- 6. 비고 ----------------
    story.append(P(tx["s6"], "h1"))
    for n in (tx["n1"], tx["n2"], tx["n3"].format(scale=f"{scale_used:g}"), tx["n4"]):
        story.append(P("&bull; " + n, "body"))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(fnt_reg, 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(18 * mm, 10 * mm, f"{M.path.name}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{tx['page']} {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(out_pdf), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=15 * mm,
                            bottomMargin=16 * mm, title=tx["title"], author="rmed_report.py")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


# ============================================================================
# 6. main
# ============================================================================
def main(argv=None):
    import tkinter as tk
    from tkinter import filedialog

    ap = argparse.ArgumentParser(description="Code_Aster .rmed -> PDF report",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    
    # 수정: rmed 인자를 선택적(nargs="?", default=None)으로 변경하여 창을 띄울 수 있게 만듭니다.
    ap.add_argument("rmed", nargs="?", default=None, help="입력 .rmed 파일 (생략 시 윈도우 파일 선택창 실행)")
    
    ap.add_argument("-o", "--outdir", default="report_out", help="출력 폴더 (PDF + 이미지)")
    ap.add_argument("--lang", choices=["ko", "en"], default="ko", help="리포트 언어")
    ap.add_argument("--font", default=None, help="한글 TrueType(.ttf) 경로 (자동 탐색 실패 시)")
    g = ap.add_argument_group("재질 (rmed 에는 저장되어 있지 않음)")
    g.add_argument("--comm", default=None, help="Code_Aster .comm 파일 (DEFI_MATERIAU 에서 E, NU, RHO, SY 추출)")
    g.add_argument("--mat-name", default=None, help="재질명")
    g.add_argument("--sy", type=float, default=None, help="항복강도 Yield strength [MPa]")
    g.add_argument("--E", type=float, default=None, help="탄성계수 [GPa]")
    g.add_argument("--nu", type=float, default=None, help="포아송비")
    g.add_argument("--rho", type=float, default=None, help="밀도 [kg/m3]")
    u = ap.add_argument_group("단위 / 보기")
    u.add_argument("--length-unit", choices=list(LENGTH_TO_MM), default="m", help="모델 길이 단위")
    u.add_argument("--stress-unit", choices=list(STRESS_TO_MPA), default="Pa", help="모델 응력 단위")
    u.add_argument("--scale", default="1", help="변형 배율 (숫자 또는 'auto')")
    u.add_argument("--az", type=float, default=-60.0, help="카메라 방위각 [deg]")
    u.add_argument("--el", type=float, default=25.0, help="카메라 고도각 [deg]")
    u.add_argument("--step", type=int, default=-1, help="결과 스텝 인덱스 (-1 = 마지막)")
    u.add_argument("--disp-field", default=None, help="변위 필드명 (기본: *DEPL 자동 탐색)")
    u.add_argument("--stress-field", default=None, help="응력 필드명 (기본: *SIEQ_NOEU 자동 탐색)")
    args = ap.parse_args(argv)

    # 추가: 인자로 rmed 파일이 주어지지 않은 경우 파일 선택창 호출
    if args.rmed is None:
        root = tk.Tk()
        root.withdraw()  # 빈 GUI 메인 창 숨김
        root.attributes('-topmost', True)  # 선택창을 화면 최상단으로 강제
        
        file_path = filedialog.askopenfilename(
            title="Code_Aster 결과 파일(.rmed)을 선택하세요",
            filetypes=[("RMED Files", "*.rmed"), ("MED Files", "*.med"), ("All Files", "*.*")]
        )
        
        # 파일 선택창을 닫거나 취소한 경우 프로그램 종료
        if not file_path:
            print("[INFO] 파일 선택이 취소되었습니다. 프로그램을 종료합니다.")
            sys.exit(0)
            
        args.rmed = file_path
        root.destroy()

    t0 = time.time()
    out = Path(args.outdir)
    (out / "images").mkdir(parents=True, exist_ok=True)
    L2MM, S2MPA = LENGTH_TO_MM[args.length_unit], STRESS_TO_MPA[args.stress_unit]

    print(f"[1/5] Reading {args.rmed} ...")
    M = read_med(args.rmed, args.step, args.disp_field, args.stress_field)
    vol_types = [t for t in ("T10", "TE4") if t in M.blocks]
    if not vol_types:
        sys.exit(f"[ERROR] 지원하는 체적 요소(T10/TE4)가 없습니다. 존재: {list(M.blocks)}")
    M.vol_type = vol_types[0]
    quadratic = M.vol_type == "T10"
    mat = build_material(args, S2MPA)
    print(f"      nodes={M.n_nodes:,}  {M.vol_type}={M.blocks[M.vol_type]['n']:,}  "
          f"disp='{M.disp_field}'  stress='{M.stress_field}'  yield={mat['yield_MPa']:g} MPa")

    print("[2/5] Computing statistics ...")
    R = analyze(M, mat, L2MM, S2MPA)

    print("[3/5] Extracting boundary surface ...")
    F = extract_boundary(M.X, M.blocks[M.vol_type]["conn"], quadratic)
    # 표면 그룹 매핑 (TR3/TR6 요소의 family -> 경계면)
    face_fam = np.zeros(len(F), dtype=np.int64)
    ks, fs = [], []
    for t, b in M.blocks.items():
        if t in ("TR3", "TR6"):
            ks.append(_key3(np.sort(b["conn"][:, :3], 1), M.n_nodes))
            fs.append(b["fam"])
    if ks:
        ks, fs = np.concatenate(ks), np.concatenate(fs)
        o = np.argsort(ks)
        ks, fs = ks[o], fs[o]
        fk = _key3(np.sort(F[:, :3], 1), M.n_nodes)
        pos = np.clip(np.searchsorted(ks, fk), 0, len(ks) - 1)
        face_fam = np.where(ks[pos] == fk, fs[pos], 0)
    fam_names = {k: v for k, v in M.fam_groups.items() if k != 0}

    scene = Scene(M.X, F, quadratic, args.az, args.el, face_fam, fam_names)
    Umag_model = np.linalg.norm(M.U, axis=1).max()
    size = float(np.linalg.norm(R["bbox"][1] - R["bbox"][0]))
    scale = (0.1 * size / Umag_model if Umag_model > 0 else 1.0) if args.scale == "auto" else float(args.scale)
    P_def = M.X + scale * M.U
    sc_txt = f"deformation scale x{scale:g}" + (" (auto)" if args.scale == "auto" else "")

    print("[4/5] Rendering images ...")
    img_dir = out / "images"
    figs = {k: img_dir / f"{k}.png" for k in ("mesh", "mesh_b", "disp", "vm", "split", "binary", "hist")}
    st = R["stat"]
    sig, dmag, sy = R["sig"], R["Umag"], R["sy"]

    def loc(i):
        return f"({M.X[i, 0]:.4g}, {M.X[i, 1]:.4g}, {M.X[i, 2]:.4g}) {args.length_unit}"

    def mk(stat, unit, tag):
        return [dict(kind="max", node=stat["imax"], color="#c1121f", marker="^",
                     text=f"MAX {_fmt(stat['max'])} {unit}\nN{stat['imax'] + 1}"),
                dict(kind="min", node=stat["imin"], color="#1d4ed8", marker="v",
                     text=f"MIN {_fmt(stat['min'])} {unit}\nN{stat['imin'] + 1}")]

    def hdr(stat, unit):
        return [(f"MAX  {_fmt(stat['max']):>12} {unit:<4} @ N{stat['imax'] + 1:<7} {loc(stat['imax'])}", "#c1121f"),
                (f"MIN  {_fmt(stat['min']):>12} {unit:<4} @ N{stat['imin'] + 1:<7} {loc(stat['imin'])}", "#1d4ed8"),
                (f"{sc_txt}   |   step #{M.step['NOR']}   |   view az={args.az:g}, el={args.el:g}", "#555555")]

    tx_mesh = TXT["en"]
    ne_by = ", ".join(f"{t}: {b['n']:,}" for t, b in sorted(M.blocks.items(), key=lambda kv: -kv[1]["dim"]))
    render_mesh(scene, figs["mesh"], "Mesh & boundary groups",
                [(f"Nodes: {M.n_nodes:,}   |   Elements  {ne_by}", "#333333"),
                 (f"Solid element type: {M.vol_type} ({M.blocks[M.vol_type]['desc']})", "#333333")],
                face_fam, fam_names, "ungrouped surface")
    scene_b = Scene(M.X, F, quadratic, args.az + 180.0, args.el, face_fam, fam_names)
    render_mesh(scene_b, figs["mesh_b"], "Mesh & boundary groups (opposite view)",
                [(f"Camera: az={args.az + 180:g}, el={args.el:g}", "#333333")],
                face_fam, fam_names, "ungrouped surface")
    render_scalar(scene, P_def, dmag, figs["disp"], mode="continuous", title="Displacement magnitude |U| [mm]",
                  header_lines=hdr(st["|U|"], "mm"), vmin=dmag.min(), vmax=dmag.max(), label="|U| [mm]",
                  marks=mk(st["|U|"], "mm", "U"))
    render_scalar(scene, P_def, sig, figs["vm"], mode="continuous", title="von Mises stress [MPa]",
                  header_lines=hdr(st["VMIS"], "MPa"), vmin=sig.min(), vmax=sig.max(), label="von Mises [MPa]",
                  marks=mk(st["VMIS"], "MPa", "S"))
    yl = [(f"Yield strength  sigma_y = {sy:g} MPa   ({mat['name']})", "#111111"),
          (f"MAX von Mises = {_fmt(st['VMIS']['max'])} MPa   ->   safety factor = {R['safety_factor']:.3f}", "#c1121f"),
          (f"At/above yield:  nodes {R['nodes_above_pct']:.1f} %   |   volume {R['volume_above_pct']:.1f} %", "#555555"),
          (f"{sc_txt}   |   step #{M.step['NOR']}", "#555555")]
    render_scalar(scene, P_def, sig, figs["split"], mode="split", title="von Mises stress - yield-split color scale",
                  header_lines=yl, vmin=sig.min(), vmax=sig.max(), label="von Mises [MPa]", sy=sy,
                  marks=mk(st["VMIS"], "MPa", "S")[:1])
    render_scalar(scene, P_def, sig, figs["binary"], mode="binary", title="von Mises stress - below / above yield",
                  header_lines=yl, vmin=sig.min(), vmax=sig.max(), label="", sy=sy,
                  binary_labels=(f"below yield (< {sy:g} MPa)", f"at/above yield (>= {sy:g} MPa)"))
    render_hist(sig, sy, figs["hist"], "von Mises [MPa]")

    print("[5/5] Building PDF ...")
    lang, fr, fb = setup_fonts(args.lang, args.font)
    pdf = out / f"report_{Path(args.rmed).stem}.pdf"
    build_pdf(pdf, M, mat, R, figs, args, lang, fr, fb, scale)

    summary = dict(file=M.path.name, nodes=M.n_nodes, volume_element=M.vol_type,
                   n_volume_elements=int(M.blocks[M.vol_type]["n"]), material={k: v for k, v in mat.items()},
                   displacement_mm={k: R["stat"][k] for k in ("|U|", "DX", "DY", "DZ")},
                   von_mises_MPa=R["stat"]["VMIS"], yield_MPa=sy, safety_factor=R["safety_factor"],
                   nodes_above_yield_pct=R["nodes_above_pct"], volume_above_yield_pct=R["volume_above_pct"])
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Done in {time.time() - t0:.1f}s ->  {pdf}")
    return pdf


if __name__ == "__main__":
    main()