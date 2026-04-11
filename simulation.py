"""
FAB 플릿 시뮬레이션 — ACS: 그래프 주행, 교행 양보, 자동 충전, 작업 진행률.
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from wave_map import get_config

# ─── 파일 로거 설정 ───────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulation.log")
_file_logger = logging.getLogger("sim_file")
_file_logger.setLevel(logging.DEBUG)
if not _file_logger.handlers:
    _fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    _file_logger.addHandler(_fh)
    _file_logger.propagate = False

def _flog(level: str, msg: str) -> None:
    getattr(_file_logger, level, _file_logger.info)(msg)

BATTERY_COST_PER_METER = 1.15


def get_battery_cost_per_meter() -> float:
    """실제 소모율과 예측값을 항상 일치시킴."""
    nav = float(sim_params.get("nav_speed", 1.35))
    drain = float(sim_params.get("battery_drain_running", 1.55))
    return drain / max(nav, 0.01)


MIN_BATTERY_AFTER_TASK_PCT = 5.0
ARRIVAL_EPS = 0.14
CHARGE_TRIGGER_PCT = 40.0      # 기존 20.0 → 운영 하한 40%
CHARGE_COMPLETE_PCT = 90.0     # 기존 80.0 → 운영 상한 90%
LOW_BATTERY_FORCE_CHARGE_PCT = 35.0  # 기존 15.0 → 35% 이하 강제 충전
CONFLICT_DIST_M = 0.42
SAFETY_MARGIN_PCT = 10.0
CRITICAL_BATTERY_PCT = 8.0

sim_params: Dict[str, Any] = {
    "nav_speed": 1.15,                      # 실제 평균 운영속도 1.1~1.2m/s 중간값
    "battery_drain_running": 0.00333,       # 12%/h = 0.00333%/s
    "battery_drain_idle": 0.00222,          # 8%/h = 0.00222%/s
    "battery_drain_running_nopower": 0.00222, # 교행 대기 = idle과 동일
    "battery_charge_rate": 0.01068,         # 20A/52Ah = 0.385C → 50%/4680s
    "auto_dispatch_enabled": 1.0,
    "auto_job_interval_s": 7.0,
    "safety_margin_pct": 10.0,
    "critical_battery_pct": 8.0,
    "yield_stall_reroute_s": 3.5,
    "yield_stall_abort_s": 9.0,
    "sim_speed_multiplier": 1.0,   # 1x ~ 100x
}

# 충전기 라벨 → 예약한 AMR id (출발 시점부터 도착 시 해제)
CHARGER_RESERVATIONS: Dict[str, str] = {}

analytics: Dict[str, Any] = {
    "sim_time_s": 0.0,
    "tasks_completed": 0,
    "tasks_started": 0,
    "tasks_interrupted": 0,
    "tasks_failed": 0,
    "total_transport_m": 0.0,
    "robot_running_s": {},
    "robot_idle_s": {},
    "robot_charge_s": {},
    "robot_wait_pending_s": {},
    # 태스크 단위 시간 리스트 (초)
    "task_lead_times": [],      # created_at → done_at
    "task_run_times": [],       # started_at → done_at
    "task_queue_waits": [],     # created_at → started_at
    # 충전 이벤트 카운트
    "charge_count": 0,
    "emergency_charge_count": 0,
    # SLA (기본 120초 이내 완료 목표)
    "sla_threshold_s": 120.0,
    "sla_met_count": 0,
    "session_started_mono": time.monotonic(),
}

acs_log: Deque[Dict[str, Any]] = deque(maxlen=200)

STATIONS: Dict[str, Tuple[float, float]] = {}
CHARGING_STATIONS: frozenset = frozenset()


def _stations_from_wave() -> Dict[str, Tuple[float, float]]:
    return dict(get_config().stations_xy())


def _charging_from_wave() -> frozenset:
    return frozenset(get_config().charging_labels())


def refresh_station_cache() -> None:
    global STATIONS, CHARGING_STATIONS
    STATIONS = _stations_from_wave()
    CHARGING_STATIONS = _charging_from_wave()


refresh_station_cache()


def acs_log_event(kind: str, message: str, **extra: Any) -> None:
    entry = {
        "t_mono": time.monotonic(),
        "ts": datetime.now().isoformat(),
        "kind": kind,
        "message": message,
        **extra,
    }
    acs_log.append(entry)
    level = "error" if kind in ("error", "charge_fail") else "warning" if kind in ("critical_return", "yield_abort", "dispatch_reject") else "info"
    robot = extra.get("robot", "")
    _flog(level, f"[{kind}] {message}" + (f" | robot={robot}" if robot else ""))


def station_coords(label: str) -> Tuple[float, float]:
    return STATIONS.get(label, (12.0, 8.0))


def distance_m(x0: float, y0: float, x1: float, y1: float) -> float:
    return math.hypot(x1 - x0, y1 - y0)


def estimate_move_battery_pct(amr: Dict[str, Any], destination_label: str) -> float:
    cfg = get_config()
    gd = cfg.graph_distance_m(float(amr.get("x") or 0), float(amr.get("y") or 0), destination_label)
    if gd is not None:
        return gd * get_battery_cost_per_meter()
    x0, y0 = float(amr.get("x") or 0), float(amr.get("y") or 0)
    x1, y1 = station_coords(destination_label)
    return distance_m(x0, y0, x1, y1) * get_battery_cost_per_meter()


def nearest_charging_station(x: float, y: float) -> str:
    best: Optional[str] = None
    best_d = float("inf")
    for name in sorted(CHARGING_STATIONS):
        if name not in STATIONS:
            continue
        cx, cy = STATIONS[name]
        d = distance_m(x, y, cx, cy)
        if d < best_d:
            best_d = d
            best = name
    return best or next(iter(CHARGING_STATIONS), "Charger-1")


def release_charger_reservations_for_amr(amr_id: str) -> None:
    to_del = [k for k, v in CHARGER_RESERVATIONS.items() if v == amr_id]
    for k in to_del:
        del CHARGER_RESERVATIONS[k]


def _charger_usable_by(amr_id: str, label: str, amr_list: List[Dict[str, Any]], task_list: List[Dict[str, Any]]) -> bool:
    if label not in CHARGING_STATIONS:
        return False
    res = CHARGER_RESERVATIONS.get(label)
    if res and res != amr_id:
        return False
    for a in amr_list:
        ensure_amr_shape(a)
        if a["id"] != amr_id and a.get("location") == label:
            return False
    for t in task_list:
        if t.get("destination") != label:
            continue
        oid = str(t.get("amr_id") or "")
        if not oid or oid == amr_id:
            continue
        if t.get("status") in ("pending", "running"):
            return False
    return True


def nearest_free_charger(
    amr_id: str,
    x: float,
    y: float,
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
    exclude: Optional[Set[str]] = None,
) -> Optional[str]:
    """예약·타 로봇 점유·진행 중인 타 충전 미션을 제외한 가장 가까운 충전기."""
    if not CHARGING_STATIONS:
        return None
    ex = exclude or set()
    cfg = get_config()
    scored: List[Tuple[float, str]] = []
    for name in sorted(CHARGING_STATIONS):
        if name in ex:
            continue
        if name not in STATIONS:
            continue
        if not _charger_usable_by(amr_id, name, amr_list, task_list):
            continue
        gd = cfg.graph_distance_m(x, y, name)
        if gd is not None:
            scored.append((gd, name))
    if not scored:
        return None
    scored.sort(key=lambda z: z[0])
    return scored[0][1]


def graph_chain_distance_m(
    x0: float,
    y0: float,
    labels: List[str],
) -> Optional[float]:
    """(x0,y0)에서 labels[0]→labels[1]→… 그래프 거리 합."""
    if not labels:
        return 0.0
    cfg = get_config()
    x, y = x0, y0
    total = 0.0
    for lab in labels:
        if lab not in STATIONS:
            return None
        d = cfg.graph_distance_m(x, y, lab)
        if d is None:
            return None
        total += d
        x, y = station_coords(lab)
    return total


def get_task_pickup_drop(task: Dict[str, Any], amr: Dict[str, Any]) -> Tuple[str, str]:
    drop = str(task.get("drop_station") or task.get("destination") or "")
    pickup = str(
        task.get("pickup_station")
        or task.get("origin")
        or amr.get("location")
        or "",
    )
    if not drop:
        drop = pickup
    if not pickup:
        pickup = str(amr.get("location") or "")
    return pickup, drop


def evaluate_transport_energy_budget(
    amr: Dict[str, Any],
    pickup: str,
    drop: str,
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> Tuple[bool, Optional[str], float, float]:
    """(통과 여부, 선택 충전기, 예상 소모%, 완료 후 잔량 예측)."""
    ensure_amr_shape(amr)
    refresh_station_cache()
    bat = float(amr.get("battery") or 0.0)
    margin = float(sim_params.get("safety_margin_pct", SAFETY_MARGIN_PCT))
    ch = nearest_free_charger(amr["id"], float(amr["x"]), float(amr["y"]), amr_list, task_list)
    if not ch:
        return False, None, 0.0, bat
    chain = [pickup, drop, ch]
    dm = graph_chain_distance_m(float(amr["x"]), float(amr["y"]), chain)
    if dm is None:
        return False, ch, 0.0, bat
    cost_pct = dm * get_battery_cost_per_meter()
    after = bat - cost_pct
    if cost_pct > bat or after < margin:
        return False, ch, cost_pct, after
    return True, ch, cost_pct, after


def evaluate_charge_leg_budget(
    amr: Dict[str, Any],
    charger_label: str,
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> Tuple[bool, float, float]:
    """충전기까지 도달 가능한지만 체크. 도착 후 충전이 시작되므로 margin 불필요."""
    bat = float(amr.get("battery") or 0.0)
    dm = graph_chain_distance_m(float(amr["x"]), float(amr["y"]), [charger_label])
    if dm is None:
        return False, 0.0, bat
    cost_pct = dm * get_battery_cost_per_meter()
    after = bat - cost_pct
    if cost_pct >= bat:  # 배터리가 소모량보다 적으면 도착 불가
        return False, cost_pct, after
    return True, cost_pct, after


def nearest_non_charger_station(x: float, y: float) -> Optional[str]:
    best: Optional[str] = None
    best_d = float("inf")
    cfg = get_config()
    for lab in sorted(STATIONS.keys()):
        if lab in CHARGING_STATIONS:
            continue
        gd = cfg.graph_distance_m(x, y, lab)
        if gd is None:
            continue
        if gd < best_d:
            best_d = gd
            best = lab
    return best


def path_polyline_remaining_m(amr: Dict[str, Any]) -> float:
    """현재 위치에서 남은 경로 길이 (m)."""
    pq = amr.get("path_queue") or []
    if not pq:
        return 0.0
    x, y = float(amr["x"]), float(amr["y"])
    total = 0.0
    px, py = float(pq[0][0]), float(pq[0][1])
    total += distance_m(x, y, px, py)
    for i in range(1, len(pq)):
        ax, ay = float(pq[i - 1][0]), float(pq[i - 1][1])
        bx, by = float(pq[i][0]), float(pq[i][1])
        total += distance_m(ax, ay, bx, by)
    return total


def path_polyline_total_m(amr: Dict[str, Any]) -> float:
    """시작 시점 전체 경로 길이 근사 (첫 프레임 스냅샷 task에 저장 권장)."""
    return path_polyline_remaining_m(amr) + 1e-6


def ensure_amr_shape(amr: Dict[str, Any]) -> None:
    sid = str(amr.get("id") or "")
    loc = str(amr.get("location") or "")
    st = str(amr.get("status") or "idle")
    amr["id"] = sid
    amr["location"] = loc if loc else "Unknown"
    amr["status"] = st if st else "idle"
    if "active_task_id" not in amr:
        amr["active_task_id"] = None
    if "charge_after_job" not in amr:
        amr["charge_after_job"] = False
    if "acs_yield" not in amr:
        amr["acs_yield"] = False
    if "low_bat_flag" not in amr:
        amr["low_bat_flag"] = False
    if "yield_accum_s" not in amr:
        amr["yield_accum_s"] = 0.0
    if "reroute_tried" not in amr:
        amr["reroute_tried"] = False
    try:
        bat = float(amr.get("battery"))
    except (TypeError, ValueError):
        bat = 0.0
    amr["battery"] = max(0.0, min(100.0, bat))
    for k, default in (("yaw", 0.0), ("vx", 0.0), ("vy", 0.0)):
        try:
            amr[k] = float(amr.get(k, default))
        except (TypeError, ValueError):
            amr[k] = default
    for k in ("x", "y"):
        v = amr.get(k)
        if v is None:
            if loc:
                gx, gy = station_coords(loc)
                amr["x"] = gx
                amr["y"] = gy
            else:
                amr["x"] = amr["y"] = 0.0
        else:
            try:
                amr[k] = float(v)
            except (TypeError, ValueError):
                amr["x"] = amr["y"] = 0.0
    if not isinstance(amr.get("path_queue"), list):
        amr["path_queue"] = []
    else:
        pq = []
        for p in amr["path_queue"]:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pq.append([float(p[0]), float(p[1])])
        amr["path_queue"] = pq
    if amr.get("nav_target_x") is not None:
        try:
            amr["nav_target_x"] = float(amr["nav_target_x"])
        except (TypeError, ValueError):
            amr["nav_target_x"] = None
    if amr.get("nav_target_y") is not None:
        try:
            amr["nav_target_y"] = float(amr["nav_target_y"])
        except (TypeError, ValueError):
            amr["nav_target_y"] = None


def compute_acs_state(amr: Dict[str, Any]) -> str:
    """Idle | Moving | Busy | Charging | Error"""
    ensure_amr_shape(amr)
    if str(amr.get("status") or "") == "error":
        return "Error"
    if float(amr.get("battery", 0)) <= 0:
        return "Error"
    st = amr["status"]
    if st == "charging":
        return "Charging"
    if st == "running":
        pq = amr.get("path_queue") or []
        if pq:
            return "Moving"
        return "Busy"
    return "Idle"


def station_holder(
    station: str,
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> Optional[str]:
    for a in amr_list:
        ensure_amr_shape(a)
        if a.get("location") == station:
            return a["id"]
    for t in task_list:
        if t.get("status") != "pending":
            continue
        if t.get("destination") == station:
            return str(t.get("amr_id", "")) or None
    return None


def station_claimed_by_other(
    destination: str,
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
    requester_amr_id: str,
) -> Optional[str]:
    for a in amr_list:
        ensure_amr_shape(a)
        if a.get("location") == destination and a["id"] != requester_amr_id:
            return a["id"]
    res = CHARGER_RESERVATIONS.get(destination)
    if res and res != requester_amr_id:
        return res
    for t in task_list:
        if t.get("destination") != destination:
            continue
        oid = str(t.get("amr_id") or "")
        if not oid or oid == requester_amr_id:
            continue
        if t.get("status") in ("pending", "running"):
            return oid
    return None


def make_amr(amr_id: str, status: str, location: str, battery: float) -> Dict[str, Any]:
    refresh_station_cache()
    x, y = station_coords(location)
    return {
        "id": amr_id,
        "status": status,
        "location": location,
        "battery": float(battery),
        "x": x,
        "y": y,
        "yaw": 0.0,
        "vx": 0.0,
        "vy": 0.0,
        "nav_target_x": None,
        "nav_target_y": None,
        "path_queue": [],
        "active_task_id": None,
        "charge_after_job": False,
        "acs_yield": False,
        "low_bat_flag": False,
        "yield_accum_s": 0.0,
        "reroute_tried": False,
    }


def set_nav_path_for_destination(amr: Dict[str, Any], destination_label: str) -> bool:
    ensure_amr_shape(amr)
    cfg = get_config()
    pts = cfg.path_for_labels(float(amr["x"]), float(amr["y"]), destination_label)
    if not pts:
        return False
    amr["path_queue"] = [[px, py] for px, py in pts]
    amr["nav_target_x"] = None
    amr["nav_target_y"] = None
    return True


def clear_nav(amr: Dict[str, Any]) -> None:
    amr["nav_target_x"] = None
    amr["nav_target_y"] = None
    amr["path_queue"] = []


def snap_amr_to_location(amr: Dict[str, Any]) -> None:
    ensure_amr_shape(amr)
    lx, ly = station_coords(str(amr.get("location", "")))
    amr["x"], amr["y"] = lx, ly
    amr["vx"] = amr["vy"] = 0.0
    clear_nav(amr)


def start_task_for_amr(
    task: Dict[str, Any],
    amr: Dict[str, Any],
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> Optional[str]:
    """pending → running, 그래프 경로 계획. 성공 시 None, 실패 시 한글 메시지."""
    ensure_amr_shape(amr)
    refresh_station_cache()
    if task.get("status") != "pending":
        return "pending 상태인 작업만 시작할 수 있습니다"
    dest = str(task.get("destination") or "")
    if dest not in STATIONS:
        return "알 수 없는 목적지입니다"
    other = station_claimed_by_other(dest, amr_list, task_list, amr["id"])
    if other and other != amr["id"]:
        return f"스테이션 '{dest}'이(가) {other}에 의해 점유·예약되어 있습니다"
    if str(amr.get("status") or "") == "error":
        return "AMR이 오류 상태입니다"
    if amr["status"] != "idle":
        return f"{amr['id']}가 idle 상태가 아닙니다"
    bat = float(amr.get("battery") or 0.0)
    if bat <= 0.0:
        return "배터리가 없습니다"
    if bat <= LOW_BATTERY_FORCE_CHARGE_PCT and dest not in CHARGING_STATIONS:
        suggest = nearest_free_charger(amr["id"], float(amr["x"]), float(amr["y"]), amr_list, task_list)
        sl = suggest or nearest_charging_station(float(amr["x"]), float(amr["y"]))
        return (
            f"배터리가 {LOW_BATTERY_FORCE_CHARGE_PCT}% 이하입니다. "
            f"충전 스테이션(권장: {sl})으로의 이동만 가능합니다."
        )

    if dest in CHARGING_STATIONS:
        ok_leg, cost_pct, after = evaluate_charge_leg_budget(amr, dest, amr_list, task_list)
        if not ok_leg:
            alt = nearest_free_charger(amr["id"], float(amr["x"]), float(amr["y"]), amr_list, task_list)
            if alt and alt != dest:
                dest = alt
                task["destination"] = alt
                ok_leg, cost_pct, after = evaluate_charge_leg_budget(amr, dest, amr_list, task_list)
            if not ok_leg:
                _begin_charge_dispatch(amr, alt or dest, amr_list, task_list, force=True)
                task["status"] = "rejected"
                task["done_at"] = datetime.now().isoformat()
                task["reject_reason"] = "charge_leg_energy"
                return (
                    f"충전기까지 에너지 부족(예상 {cost_pct:.1f}% 소모, 잔량 {after:.1f}%). "
                    "긴급 충전 미션을 발행했습니다."
                )
    else:
        pickup, drop = get_task_pickup_drop(task, amr)
        if pickup not in STATIONS or drop not in STATIONS:
            return "상차/하차 스테이션 정보가 올바르지 않습니다"
        ok_b, plan_ch, cost_pct, after = evaluate_transport_energy_budget(
            amr, pickup, drop, amr_list, task_list
        )
        if not ok_b:
            task["status"] = "rejected"
            task["done_at"] = datetime.now().isoformat()
            task["reject_reason"] = "energy_budget"
            task["planned_charger_if_reject"] = plan_ch
            acs_log_event(
                "energy_reject",
                f"{amr['id']} 예산 거부 (소모~{cost_pct:.1f}%, 잔량예측 {after:.1f}%) → 충전",
                robot=amr["id"],
                job_id=task.get("task_id"),
            )
            if plan_ch:
                _begin_charge_dispatch(amr, plan_ch, amr_list, task_list, force=True)
            return (
                f"에너지 예산 미달: 현재→상차→하차→충전 전체 {cost_pct:.1f}% 예상, "
                f"완료 후 잔량 {after:.1f}% (안전마진 {sim_params.get('safety_margin_pct', SAFETY_MARGIN_PCT)}% 미만). "
                "작업은 거부되었고 가용 충전기로 자동 출발합니다."
            )

    prev_loc = str(amr.get("location") or "")
    task.setdefault("origin", prev_loc)
    amr["status"] = "running"
    amr["location"] = dest
    amr["active_task_id"] = task["task_id"]
    amr["yield_accum_s"] = 0.0
    amr["reroute_tried"] = False
    if not set_nav_to_destination(amr, dest):
        amr["status"] = "idle"
        amr["location"] = prev_loc or amr["location"]
        amr["active_task_id"] = None
        clear_nav(amr)
        snap_amr_to_location(amr)
        return "맵 그래프에서 목적지까지 연결된 경로가 없습니다 (A* 실패)"
    task["status"] = "running"
    task["started_at"] = datetime.now().isoformat()
    task["progress_pct"] = float(task.get("progress_pct") or 0.0)
    task["path_total_m"] = path_polyline_total_m(amr)
    record_task_started()
    acs_log_event(
        "job_started",
        f"작업 시작 · {task.get('task_id')} → {dest}",
        job_id=task.get("task_id"),
        robot=amr["id"],
    )
    return None


def try_autostart_pending(
    amr: Dict[str, Any],
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> None:
    ensure_amr_shape(amr)
    if amr["status"] != "idle":
        return
    pending = [t for t in task_list if t.get("amr_id") == amr["id"] and t.get("status") == "pending"]
    if not pending:
        return
    pending.sort(key=lambda t: (-int(t.get("priority") or 1), str(t.get("created_at") or "")))
    for t in pending:
        if start_task_for_amr(t, amr, amr_list, task_list) is None:
            return


def _current_nav_target(amr: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    pq = amr.get("path_queue") or []
    if pq and len(pq[0]) >= 2:
        return float(pq[0][0]), float(pq[0][1])
    tx, ty = amr.get("nav_target_x"), amr.get("nav_target_y")
    if tx is not None and ty is not None:
        return float(tx), float(ty)
    return None


def _advance_path(amr: Dict[str, Any]) -> None:
    pq = amr.get("path_queue") or []
    if pq:
        pq.pop(0)
        amr["path_queue"] = pq


def _apply_congestion_yield(amr_list: List[Dict[str, Any]]) -> None:
    """동일 웨이포인트로 진입 경합 시 ID 순 양보(교행 정체)."""
    running = [a for a in amr_list if a["status"] == "running" and (a.get("path_queue") or [])]
    by_wp: Dict[Tuple[int, int], List[str]] = {}
    for a in running:
        w = a["path_queue"][0]
        key = (round(w[0] * 50), round(w[1] * 50))
        by_wp.setdefault(key, []).append(a["id"])
    yield_ids: Set[str] = set()
    for aids in by_wp.values():
        if len(aids) < 2:
            continue
        aids.sort()
        yield_ids.update(aids[1:])
    ids = sorted([a["id"] for a in running])
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a1 = next(x for x in amr_list if x["id"] == ids[i])
            a2 = next(x for x in amr_list if x["id"] == ids[j])
            d = distance_m(float(a1["x"]), float(a1["y"]), float(a2["x"]), float(a2["y"]))
            if d < CONFLICT_DIST_M:
                yield_ids.add(ids[j])
    for a in amr_list:
        a["acs_yield"] = a["id"] in yield_ids


def _update_task_progress(task: Dict[str, Any], amr: Dict[str, Any]) -> None:
    total = float(task.get("path_total_m") or 0.0)
    if total <= 0:
        total = path_polyline_total_m(amr)
        task["path_total_m"] = total
    rem = path_polyline_remaining_m(amr)
    pct = 100.0 * (1.0 - min(1.0, rem / max(total, 1e-6)))
    task["progress_pct"] = round(max(0.0, min(100.0, pct)), 1)


def _complete_task(
    task: Dict[str, Any],
    amr: Dict[str, Any],
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> None:
    if task.get("task_type") == "emergency_drop":
        task["status"] = "done"
        task["done_at"] = datetime.now().isoformat()
        task["progress_pct"] = 100.0
        record_task_completed(task)
        acs_log_event(
            "emergency_drop_done",
            f"{amr['id']} 긴급 하차 완료 → 충전 복귀",
            job_id=task.get("task_id"),
            robot=amr["id"],
        )
        amr["active_task_id"] = None
        dest = str(task.get("destination") or "")
        amr["location"] = dest
        clear_nav(amr)
        snap_amr_to_location(amr)
        amr["status"] = "idle"
        release_charger_reservations_for_amr(amr["id"])
        ch = nearest_free_charger(amr["id"], float(amr["x"]), float(amr["y"]), amr_list, task_list)
        _begin_charge_dispatch(amr, ch, amr_list, task_list, force=True)
        return

    task["status"] = "done"
    task["done_at"] = datetime.now().isoformat()
    task["progress_pct"] = 100.0
    record_task_completed(task)
    acs_log_event(
        "job_completed",
        f"Job Completed · {task.get('task_id')} → {task.get('destination')}",
        job_id=task.get("task_id"),
        robot=amr["id"],
    )
    amr["active_task_id"] = None
    dest = str(task.get("destination") or "")
    amr["location"] = dest
    clear_nav(amr)
    snap_amr_to_location(amr)

    if dest in CHARGING_STATIONS:
        if CHARGER_RESERVATIONS.get(dest) == amr["id"]:
            del CHARGER_RESERVATIONS[dest]
        release_charger_reservations_for_amr(amr["id"])
        amr["status"] = "charging"
        amr["charge_after_job"] = False
        amr["low_bat_flag"] = False
        acs_log_event("charging", f"{amr['id']} 충전소 도착 → Charging", robot=amr["id"])
        return

    amr["status"] = "idle"
    need_charge = (
        float(amr["battery"]) < CHARGE_TRIGGER_PCT
        or amr.get("charge_after_job")
        or amr.get("low_bat_flag")
        or float(amr["battery"]) < LOW_BATTERY_FORCE_CHARGE_PCT
    )
    amr["charge_after_job"] = False
    if need_charge and CHARGING_STATIONS:
        ch = nearest_free_charger(amr["id"], float(amr["x"]), float(amr["y"]), amr_list, task_list)
        bat_now = float(amr.get("battery") or 0.0)
        if bat_now <= CRITICAL_BATTERY_PCT:
            # 배터리가 임계값 이하 → 이동 자체가 위험하므로 현장에서 직접 충전 시작
            rescue_warp_amr(amr["id"], ch, amr_list, task_list, min_battery_floor=0.0)
        else:
            _begin_charge_dispatch(amr, ch, amr_list, task_list, force=True)
    if amr["status"] == "idle":
        try_autostart_pending(amr, amr_list, task_list)


def _begin_charge_dispatch(
    amr: Dict[str, Any],
    charger_label: Optional[str],
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
    force: bool = False,
) -> bool:
    global _internal_job_counter
    if not CHARGING_STATIONS:
        return False
    release_charger_reservations_for_amr(amr["id"])
    cfg = get_config()
    scored: List[Tuple[float, str]] = []
    for name in sorted(CHARGING_STATIONS):
        if name not in STATIONS:
            continue
        if not _charger_usable_by(amr["id"], name, amr_list, task_list):
            continue
        gd = cfg.graph_distance_m(float(amr["x"]), float(amr["y"]), name)
        if gd is not None:
            scored.append((gd, name))
    scored.sort(key=lambda z: z[0])
    ordered = [c for _, c in scored]
    if charger_label and charger_label in STATIONS and charger_label in CHARGING_STATIONS:
        if charger_label in ordered:
            ordered = [charger_label] + [c for c in ordered if c != charger_label]
        else:
            gd0 = cfg.graph_distance_m(float(amr["x"]), float(amr["y"]), charger_label)
            if gd0 is not None and _charger_usable_by(amr["id"], charger_label, amr_list, task_list):
                ordered.insert(0, charger_label)

    origin_lab = str(amr.get("location") or "")
    for ch in ordered:
        if not _charger_usable_by(amr["id"], ch, amr_list, task_list):
            continue
        ok_leg, _, _ = evaluate_charge_leg_budget(amr, ch, amr_list, task_list)
        if not ok_leg:
            if not force:
                continue
            # force=True라도 도달 불가능한 충전기는 건너뜀 (방전 방지)
            # 단, 배터리가 아예 없으면 어디도 못 가므로 첫 번째 충전기라도 시도
            bat_now = float(amr.get("battery") or 0.0)
            if bat_now > 0.5:
                continue
        CHARGER_RESERVATIONS[ch] = amr["id"]
        tid = f"JOB-CHG-{_internal_job_counter:04d}"
        _internal_job_counter += 1
        ct = {
            "task_id": tid,
            "amr_id": amr["id"],
            "origin": origin_lab,
            "destination": ch,
            "task_type": "charge",
            "priority": 99,
            "status": "running",
            "created_at": datetime.now().isoformat(),
            "started_at": datetime.now().isoformat(),
            "done_at": None,
            "progress_pct": 0.0,
            "auto_generated": True,
            "path_total_m": 0.0,
        }
        task_list.append(ct)
        amr["status"] = "running"
        amr["location"] = ch
        amr["active_task_id"] = tid
        amr["yield_accum_s"] = 0.0
        amr["reroute_tried"] = False
        if not set_nav_path_for_destination(amr, ch):
            task_list.pop()
            if CHARGER_RESERVATIONS.get(ch) == amr["id"]:
                del CHARGER_RESERVATIONS[ch]
            amr["status"] = "idle"
            amr["location"] = origin_lab
            amr["active_task_id"] = None
            clear_nav(amr)
            snap_amr_to_location(amr)
            continue
        ct["path_total_m"] = path_polyline_total_m(amr)
        record_task_started()
        record_charge_event(force=force)
        acs_log_event(
            "auto_charge",
            f"{amr['id']} → {ch} 충전 예약·출발 (force={force})",
            robot=amr["id"],
        )
        return True
    charger_state = {c: CHARGER_RESERVATIONS.get(c, "free") for c in sorted(CHARGING_STATIONS)}
    occupied_by = {c: next((a["id"] for a in amr_list if a.get("location") == c), None) for c in CHARGING_STATIONS}
    _flog("error", f"CHARGE_FAIL {amr['id']} | bat={float(amr.get('battery',0)):.1f}% | 예약={charger_state} | 점유={occupied_by}")
    acs_log_event("charge_fail", f"{amr['id']} 충전 경로/슬롯 확보 실패", robot=amr["id"])
    return False


_internal_job_counter = 1


def _interrupt_running_transport(
    task: Dict[str, Any],
    amr: Dict[str, Any],
    reason: str,
) -> None:
    if task.get("status") == "running":
        task["status"] = "interrupted"
        task["done_at"] = datetime.now().isoformat()
        task["interrupt_reason"] = reason
        record_task_interrupted()
    amr["active_task_id"] = None
    clear_nav(amr)


def _start_emergency_drop(
    amr: Dict[str, Any],
    drop_lab: str,
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> None:
    global _internal_job_counter
    release_charger_reservations_for_amr(amr["id"])
    origin_lab = str(amr.get("location") or "")
    tid = f"JOB-EMG-{_internal_job_counter:04d}"
    _internal_job_counter += 1
    et: Dict[str, Any] = {
        "task_id": tid,
        "amr_id": amr["id"],
        "origin": origin_lab,
        "destination": drop_lab,
        "task_type": "emergency_drop",
        "priority": 100,
        "status": "running",
        "created_at": datetime.now().isoformat(),
        "started_at": datetime.now().isoformat(),
        "done_at": None,
        "progress_pct": 0.0,
        "auto_generated": True,
        "path_total_m": 0.0,
    }
    task_list.append(et)
    amr["status"] = "running"
    amr["location"] = drop_lab
    amr["active_task_id"] = tid
    amr["yield_accum_s"] = 0.0
    amr["reroute_tried"] = False
    if not set_nav_path_for_destination(amr, drop_lab):
        task_list.pop()
        amr["status"] = "idle"
        amr["location"] = origin_lab
        amr["active_task_id"] = None
        clear_nav(amr)
        snap_amr_to_location(amr)
        _begin_charge_dispatch(amr, None, amr_list, task_list, force=True)
        return
    et["path_total_m"] = path_polyline_total_m(amr)
    record_task_started()
    acs_log_event(
        "emergency_drop",
        f"{amr['id']} 긴급 하차 목표 {drop_lab}",
        robot=amr["id"],
    )


def _handle_critical_battery(
    amr: Dict[str, Any],
    task: Dict[str, Any],
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> bool:
    """비상 임계치 처리 시 True (같은 틱의 기존 주행 스텝 생략)."""
    if task.get("task_type") in ("charge", "emergency_drop"):
        return False
    crit = float(sim_params.get("critical_battery_pct", CRITICAL_BATTERY_PCT))
    if float(amr["battery"]) > crit:
        return False
    _interrupt_running_transport(task, amr, "critical_battery")
    x, y = float(amr["x"]), float(amr["y"])
    drop = nearest_non_charger_station(x, y)
    cfg = get_config()
    bat = float(amr["battery"])
    cost_drop = float("inf")
    if drop:
        dm = cfg.graph_distance_m(x, y, drop)
        if dm is not None:
            cost_drop = dm * get_battery_cost_per_meter()
    ch = nearest_free_charger(amr["id"], x, y, amr_list, task_list)
    cost_ch = float("inf")
    if ch:
        dm2 = cfg.graph_distance_m(x, y, ch)
        if dm2 is not None:
            cost_ch = dm2 * get_battery_cost_per_meter()
    dest_task = str(task.get("destination") or "")
    prefer_drop = (
        bool(drop)
        and dest_task not in CHARGING_STATIONS
        and cost_drop < bat - 0.5
        and cost_drop <= cost_ch * 1.2
    )
    if prefer_drop and drop:
        _start_emergency_drop(amr, drop, amr_list, task_list)
    else:
        _begin_charge_dispatch(amr, ch, amr_list, task_list, force=True)
    acs_log_event(
        "critical_return",
        f"{amr['id']} Critical ≤{crit}% — {'하차 우선' if prefer_drop else '충전 직행'}",
        robot=amr["id"],
    )
    return True


def _try_reroute_congestion(amr: Dict[str, Any], task: Dict[str, Any]) -> bool:
    pq = amr.get("path_queue") or []
    if len(pq) < 1:
        return False
    if task.get("task_type") in ("charge", "emergency_drop"):
        return False
    dest = str(task.get("destination") or "")
    if dest not in STATIONS:
        return False
    cfg = get_config()
    ax, ay = float(pq[0][0]), float(pq[0][1])
    avoid_nid = cfg.nearest_node_id(ax, ay)
    if not avoid_nid:
        return False
    alt = cfg.path_for_labels_avoid_nodes(float(amr["x"]), float(amr["y"]), dest, {avoid_nid})
    if not alt or len(alt) < 1:
        return False
    amr["path_queue"] = [[float(px), float(py)] for px, py in alt]
    amr["yield_accum_s"] = 0.0
    task["path_total_m"] = path_polyline_total_m(amr)
    acs_log_event("reroute", f"{amr['id']} 교행 구간 우회 재경로", robot=amr["id"])
    return True


def _abort_yield_to_charge(
    amr: Dict[str, Any],
    task: Dict[str, Any],
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> None:
    _interrupt_running_transport(task, amr, "yield_stall_abort")
    amr["status"] = "idle"
    _begin_charge_dispatch(amr, None, amr_list, task_list, force=True)
    acs_log_event("yield_abort", f"{amr['id']} 교행 정체·에너지 위험 — 충전 복귀", robot=amr["id"])


def _handle_arrivals_and_charging(
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> None:
    for amr in amr_list:
        ensure_amr_shape(amr)
        if amr["status"] != "running":
            continue
        tid = amr.get("active_task_id")
        if not tid:
            continue
        task = next((t for t in task_list if t.get("task_id") == tid), None)
        if not task or task.get("status") != "running":
            continue
        if amr.get("path_queue"):
            continue
        dest = str(task.get("destination") or "")
        tx, ty = station_coords(dest)
        if distance_m(float(amr["x"]), float(amr["y"]), tx, ty) > ARRIVAL_EPS * 3:
            continue
        _complete_task(task, amr, amr_list, task_list)

    for amr in amr_list:
        ensure_amr_shape(amr)
        if amr["status"] != "charging":
            continue
        b = float(amr["battery"])
        if b >= CHARGE_COMPLETE_PCT:
            amr["status"] = "idle"
            amr["low_bat_flag"] = False
            acs_log_event(
                "charge_done",
                f"{amr['id']} 충전 완료(≥{CHARGE_COMPLETE_PCT}%) → Idle",
                robot=amr["id"],
            )
            try_autostart_pending(amr, amr_list, task_list)


_tick_log_accum: float = 0.0
_TICK_LOG_INTERVAL = 5.0  # 5초마다 배터리 스냅샷 기록

def tick(amr_list: List[Dict[str, Any]], dt: float, task_list: Optional[List[Dict[str, Any]]] = None) -> None:
    multiplier = float(sim_params.get("sim_speed_multiplier", 1.0))
    multiplier = max(1.0, min(100.0, multiplier))
    dt = dt * multiplier   # 내부 시뮬 시간만 배속
    global _tick_log_accum
    refresh_station_cache()
    nav = float(sim_params.get("nav_speed", 1.35))
    dr_run = float(sim_params.get("battery_drain_running", 1.55))
    dr_idle = float(sim_params.get("battery_drain_idle", 0.3))
    dr_nop = float(sim_params.get("battery_drain_running_nopower", 0.8))
    chg = float(sim_params.get("battery_charge_rate", 14.0))

    analytics["sim_time_s"] = float(analytics.get("sim_time_s", 0.0)) + dt
    tl = task_list or []

    # 주기적 배터리·상태 스냅샷
    _tick_log_accum += dt
    if _tick_log_accum >= _TICK_LOG_INTERVAL:
        _tick_log_accum = 0.0
        charger_slots = {c: CHARGER_RESERVATIONS.get(c, "free") for c in sorted(CHARGING_STATIONS)}
        parts = [f"{a['id']}:{a.get('status','?')}:{float(a.get('battery',0)):.1f}%" for a in amr_list]
        _flog("info", "SNAPSHOT | " + " | ".join(parts))
        _flog("info", f"CHARGERS | {charger_slots}")

    _apply_congestion_yield(amr_list)

    for amr in amr_list:
        ensure_amr_shape(amr)
        aid = amr["id"]
        analytics["robot_running_s"].setdefault(aid, 0.0)
        analytics["robot_idle_s"].setdefault(aid, 0.0)
        analytics["robot_charge_s"].setdefault(aid, 0.0)
        analytics["robot_wait_pending_s"].setdefault(aid, 0.0)

        st = amr["status"]
        b = float(amr["battery"])

        # error 상태 AMR은 모든 처리를 건너뜀 (중복 로그 방지)
        if st == "error":
            continue

        has_pending = any(
            t.get("amr_id") == aid and t.get("status") == "pending" for t in tl
        )
        if st == "idle" and has_pending:
            analytics["robot_wait_pending_s"][aid] += dt
        elif st == "idle":
            analytics["robot_idle_s"][aid] += dt
        elif st == "running":
            analytics["robot_running_s"][aid] += dt

        if b <= 0.0:
            amr["battery"] = 0.0
            amr["vx"] = amr["vy"] = 0.0
            clear_nav(amr)
            rtid = amr.get("active_task_id")
            if rtid:
                rt = next((x for x in tl if x.get("task_id") == rtid), None)
                if rt and rt.get("status") == "running":
                    rt["status"] = "failed"
                    rt["done_at"] = datetime.now().isoformat()
                    record_task_failed()
            amr["active_task_id"] = None
            amr["status"] = "error"
            release_charger_reservations_for_amr(aid)  # 충전기 예약 즉시 해제
            charger_state = {c: CHARGER_RESERVATIONS.get(c, "free") for c in sorted(CHARGING_STATIONS)}
            occupied = [a["id"] for a in amr_list if a.get("status") == "charging"]
            _flog("error", f"DEAD {aid} | 배터리 방전 | 충전기예약={charger_state} | 충전중AMR={occupied}")
            acs_log_event("error", f"{aid} 배터리 방전 — Error", robot=aid)
            continue

        if st == "running":
            tid = amr.get("active_task_id")
            task = next((t for t in tl if t.get("task_id") == tid), None) if tid else None
            rr_s = float(sim_params.get("yield_stall_reroute_s", 3.5))
            ab_s = float(sim_params.get("yield_stall_abort_s", 9.0))
            margin = float(sim_params.get("safety_margin_pct", SAFETY_MARGIN_PCT))

            if task and task.get("status") == "running":
                if _handle_critical_battery(amr, task, amr_list, tl):
                    amr["battery"] = round(float(amr.get("battery", b)), 2)
                    continue

            tid = amr.get("active_task_id")
            task = next((t for t in tl if t.get("task_id") == tid), None) if tid else None
            if task and task.get("status") == "running":
                _update_task_progress(task, amr)

            if task and task.get("status") == "running":
                if amr.get("acs_yield"):
                    amr["yield_accum_s"] = float(amr.get("yield_accum_s") or 0) + dt
                    ya = float(amr.get("yield_accum_s") or 0)
                    if ya >= rr_s and not amr.get("reroute_tried"):
                        _try_reroute_congestion(amr, task)
                        amr["reroute_tried"] = True
                    rem_cost = path_polyline_remaining_m(amr) * get_battery_cost_per_meter()
                    if ya >= ab_s or (ya >= 1.0 and rem_cost > max(b - margin, 0.0)):
                        _abort_yield_to_charge(amr, task, amr_list, tl)
                        amr["battery"] = round(float(amr.get("battery", b)), 2)
                        continue
                else:
                    amr["yield_accum_s"] = 0.0
                    amr["reroute_tried"] = False

            if b < CHARGE_TRIGGER_PCT:
                amr["low_bat_flag"] = True
                amr["charge_after_job"] = True

            if amr["status"] != "running":
                amr["vx"] = amr["vy"] = 0.0
                b = max(0.0, min(100.0, b - dr_nop * dt))
            else:
                target = _current_nav_target(amr)
                if target is not None and b > 0.0 and not amr.get("acs_yield"):
                    tx, ty = target
                    x, y = float(amr["x"]), float(amr["y"])
                    dx, dy = tx - x, ty - y
                    dist = math.hypot(dx, dy)
                    if dist < ARRIVAL_EPS:
                        analytics["total_transport_m"] = float(analytics.get("total_transport_m", 0.0)) + dist
                        amr["x"], amr["y"] = tx, ty
                        amr["vx"] = amr["vy"] = 0.0
                        amr["yaw"] = math.atan2(dy, max(dist, 1e-9))
                        _advance_path(amr)
                        nxt = _current_nav_target(amr)
                        if nxt is None:
                            amr["nav_target_x"] = None
                            amr["nav_target_y"] = None
                    else:
                        step = min(nav * dt, dist)
                        ux, uy = dx / dist, dy / dist
                        analytics["total_transport_m"] = float(analytics.get("total_transport_m", 0.0)) + step
                        amr["x"] = x + ux * step
                        amr["y"] = y + uy * step
                        amr["vx"] = ux * nav
                        amr["vy"] = uy * nav
                        amr["yaw"] = math.atan2(dy, dx)
                    b = max(0.0, min(100.0, b - dr_run * dt))
                else:
                    amr["vx"] = amr["vy"] = 0.0
                    b = max(0.0, min(100.0, b - dr_nop * dt))
        elif st == "charging":
            amr["vx"] = amr["vy"] = 0.0
            analytics["robot_charge_s"][aid] = float(analytics["robot_charge_s"].get(aid, 0.0)) + dt
            loc = amr.get("location") or ""
            if loc in CHARGING_STATIONS:
                cx, cy = station_coords(loc)
                amr["x"], amr["y"] = cx, cy
            b = max(0.0, min(100.0, b + chg * dt))
        elif st == "error":
            amr["vx"] = amr["vy"] = 0.0
            b = max(0.0, min(100.0, b))
        else:
            # idle 상태에서 저배터리 → 자동 충전 dispatch
            if b < CHARGE_TRIGGER_PCT and not amr.get("active_task_id"):
                already = any(
                    t.get("amr_id") == aid
                    and t.get("task_type") == "charge"
                    and t.get("status") in ("pending", "running")
                    for t in tl
                )
                if not already:
                    _begin_charge_dispatch(amr, None, amr_list, tl, force=True)
            amr["vx"] = amr["vy"] = 0.0
            b = max(0.0, min(100.0, b - dr_idle * dt))

        amr["battery"] = round(b, 2)

        if amr["battery"] <= 0.0:
            amr["battery"] = 0.0
            amr["vx"] = amr["vy"] = 0.0
            clear_nav(amr)
            amr["active_task_id"] = None

    _handle_arrivals_and_charging(amr_list, tl)

    for a in amr_list:
        a["acs_state"] = compute_acs_state(a)


def record_task_started() -> None:
    analytics["tasks_started"] = int(analytics.get("tasks_started", 0)) + 1


def record_task_completed(task: Optional[Dict[str, Any]] = None) -> None:
    analytics["tasks_completed"] = int(analytics.get("tasks_completed", 0)) + 1
    if task is None:
        return
    try:
        done_str = task.get("done_at") or datetime.now().isoformat()
        created_str = task.get("created_at")
        started_str = task.get("started_at")
        done_dt = datetime.fromisoformat(done_str)
        if created_str:
            lead = (done_dt - datetime.fromisoformat(created_str)).total_seconds()
            if 0 <= lead < 86400:
                analytics["task_lead_times"].append(round(lead, 2))
                if lead <= float(analytics.get("sla_threshold_s", 120.0)):
                    analytics["sla_met_count"] = int(analytics.get("sla_met_count", 0)) + 1
        if started_str:
            run = (done_dt - datetime.fromisoformat(started_str)).total_seconds()
            if 0 <= run < 86400:
                analytics["task_run_times"].append(round(run, 2))
        if created_str and started_str:
            wait = (datetime.fromisoformat(started_str) - datetime.fromisoformat(created_str)).total_seconds()
            if 0 <= wait < 86400:
                analytics["task_queue_waits"].append(round(wait, 2))
    except Exception:
        pass


def record_task_interrupted() -> None:
    analytics["tasks_interrupted"] = int(analytics.get("tasks_interrupted", 0)) + 1


def record_task_failed() -> None:
    analytics["tasks_failed"] = int(analytics.get("tasks_failed", 0)) + 1


def record_charge_event(force: bool = False) -> None:
    analytics["charge_count"] = int(analytics.get("charge_count", 0)) + 1
    if force:
        analytics["emergency_charge_count"] = int(analytics.get("emergency_charge_count", 0)) + 1


def _avg(lst: list) -> float:
    return round(sum(lst) / len(lst), 2) if lst else 0.0


def build_analytics_summary(amr_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    t = max(float(analytics.get("sim_time_s", 0.0)), 1e-6)
    n = max(len(amr_list), 1)
    run_sum = sum(float(analytics["robot_running_s"].get(a["id"], 0.0)) for a in amr_list)
    idle_sum = sum(float(analytics["robot_idle_s"].get(a["id"], 0.0)) for a in amr_list)
    charge_sum = sum(float(analytics["robot_charge_s"].get(a["id"], 0.0)) for a in amr_list)
    wait_sum = sum(float(analytics["robot_wait_pending_s"].get(a["id"], 0.0)) for a in amr_list)
    capacity = t * n

    completed = int(analytics.get("tasks_completed", 0))
    started = int(analytics.get("tasks_started", 0))
    charge_cnt = int(analytics.get("charge_count", 0))
    emg_cnt = int(analytics.get("emergency_charge_count", 0))
    sla_met = int(analytics.get("sla_met_count", 0))
    lead_times = analytics.get("task_lead_times") or []
    run_times = analytics.get("task_run_times") or []
    queue_waits = analytics.get("task_queue_waits") or []

    # 개별 로봇 가동률
    robot_util = {
        a["id"]: round(100.0 * float(analytics["robot_running_s"].get(a["id"], 0.0)) / t, 2)
        for a in amr_list
    }
    robot_charge_pct = {
        a["id"]: round(100.0 * float(analytics["robot_charge_s"].get(a["id"], 0.0)) / t, 2)
        for a in amr_list
    }

    return {
        # ─── 기존 지표 ────────────────────────────────────
        "sim_time_s": round(t, 2),
        "fleet_size": n,
        "tasks_completed": completed,
        "tasks_started": started,
        "tasks_interrupted": int(analytics.get("tasks_interrupted", 0)),
        "tasks_failed": int(analytics.get("tasks_failed", 0)),
        "total_transport_m": round(float(analytics.get("total_transport_m", 0.0)), 2),
        "utilization_run_pct": round(100.0 * run_sum / capacity, 2),
        "utilization_idle_pct": round(100.0 * idle_sum / capacity, 2),
        "utilization_charge_pct": round(100.0 * charge_sum / capacity, 2),
        "avg_pending_wait_s": round(wait_sum / n, 2),
        # ─── 신규: 태스크 품질 ─────────────────────────────
        "task_completion_rate": round(completed / max(started, 1) * 100.0, 2),
        "avg_lead_time_s": _avg(lead_times),
        "avg_run_time_s": _avg(run_times),
        "avg_queue_wait_s": _avg(queue_waits),
        "p95_lead_time_s": round(sorted(lead_times)[int(len(lead_times) * 0.95)] if len(lead_times) >= 20 else (_avg(lead_times)), 2),
        "sla_threshold_s": float(analytics.get("sla_threshold_s", 120.0)),
        "sla_achievement_pct": round(sla_met / max(completed, 1) * 100.0, 2),
        # ─── 신규: 충전 전략 ──────────────────────────────
        "charge_count": charge_cnt,
        "emergency_charge_count": emg_cnt,
        "emergency_charge_ratio": round(emg_cnt / max(charge_cnt, 1) * 100.0, 2),
        # ─── 신규: 개별 로봇 ──────────────────────────────
        "robot_utilization_pct": robot_util,
        "robot_charge_pct": robot_charge_pct,
        "sim_params": dict(sim_params),
    }


def build_active_jobs(task_list: List[Dict[str, Any]], amr_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in task_list:
        st = t.get("status")
        if st not in ("pending", "running"):
            continue
        rid = t.get("amr_id")
        amr = next((a for a in amr_list if a["id"] == rid), None) if rid else None
        acs = amr.get("acs_state") if amr else "—"
        out.append(
            {
                "job_id": t.get("task_id"),
                "robot": rid,
                "origin": t.get("origin") or "—",
                "destination": t.get("destination"),
                "status": st,
                "progress_pct": float(t.get("progress_pct") or 0),
                "acs_state": acs,
                "auto": bool(t.get("auto_generated")),
            }
        )
    return out


def acs_log_snapshot() -> List[Dict[str, Any]]:
    return list(acs_log)


def _rmf_mode(acs: str, legacy_status: str) -> str:
    if acs == "Charging":
        return "MODE_CHARGING"
    if acs in ("Moving", "Busy"):
        return "MODE_MOVING"
    if acs == "Error":
        return "MODE_UNRESPONSIVE"
    return "MODE_IDLE"


def _running_task_id(amr_id: str, task_list: List[Dict[str, Any]]) -> str:
    for t in task_list:
        if t.get("amr_id") == amr_id and t.get("status") == "running":
            return str(t.get("task_id", ""))
    return ""


def build_fleet_states(amr_list: List[Dict[str, Any]], task_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    robots: List[Dict[str, Any]] = []
    for a in amr_list:
        ensure_amr_shape(a)
        battery = float(a.get("battery", 0.0))
        acs = str(a.get("acs_state") or compute_acs_state(a))
        issues: List[str] = []
        if battery < 10.0:
            issues.append("CRITICAL_BATTERY")
        elif battery < 20.0:
            issues.append("LOW_BATTERY")
        if acs == "Error":
            issues.append("ACS_ERROR")
        if str(a.get("status") or "") == "error" or battery <= 0.0:
            issues.append("RESCUE_REQUIRED")
        vx = float(a.get("vx", 0.0))
        vy = float(a.get("vy", 0.0))
        robots.append(
            {
                "name": a["id"],
                "model": "fab_diff_drive",
                "battery_percent": round(battery, 2),
                "acs_state": acs,
                "location": {
                    "map_name": "fab_l1",
                    "x": round(float(a["x"]), 4),
                    "y": round(float(a["y"]), 4),
                    "yaw": round(float(a.get("yaw", 0.0)), 4),
                    "idx": 0,
                    "speed": round(math.hypot(vx, vy), 4),
                    "obey_advisory_speed_limit": True,
                },
                "mode": _rmf_mode(acs, str(a.get("status", "idle"))),
                "task_id": _running_task_id(a["id"], task_list),
                "issues": issues,
            }
        )
    return {
        "name": "fab_amr_fleet",
        "fleet_name": "fab_amr_fleet",
        "timestamp_ms": int(time.time() * 1000),
        "robots": robots,
    }


def build_station_overlay(
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
) -> Dict[str, Any]:
    refresh_station_cache()
    out: Dict[str, Any] = {}
    for name in STATIONS:
        holder = station_holder(name, amr_list, task_list)
        res = CHARGER_RESERVATIONS.get(name) if name in CHARGING_STATIONS else None
        out[name] = {
            "occupied": holder is not None,
            "by": holder,
            "charging": name in CHARGING_STATIONS,
            "reserved_by": res,
        }
    return out


def rescue_warp_amr(
    amr_id: str,
    charger_label: Optional[str],
    amr_list: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
    min_battery_floor: float = 14.0,
) -> Optional[str]:
    """Error 상태 AMR을 충전기로 워프(현장 수동 구조 모사). 성공 시 None."""
    amr = next((a for a in amr_list if a["id"] == amr_id), None)
    if not amr:
        return "AMR을 찾을 수 없습니다"
    ensure_amr_shape(amr)
    refresh_station_cache()
    release_charger_reservations_for_amr(amr_id)
    for t in task_list:
        if t.get("amr_id") != amr_id:
            continue
        if t.get("status") == "running":
            t["status"] = "interrupted"
            t["done_at"] = datetime.now().isoformat()
            t["interrupt_reason"] = "rescue_warp"
    amr["active_task_id"] = None
    clear_nav(amr)
    ch = (charger_label or "").strip() or None
    if ch and ch not in CHARGING_STATIONS:
        ch = None
    ch = ch or nearest_free_charger(amr_id, float(amr["x"]), float(amr["y"]), amr_list, task_list)
    if not ch:
        return "가용 충전기가 없습니다"
    amr["status"] = "charging"
    amr["location"] = ch
    amr["battery"] = max(float(amr.get("battery") or 0.0), min_battery_floor)
    snap_amr_to_location(amr)
    acs_log_event(
        "rescue_warp",
        f"{amr_id} 관제 워프 → {ch} (배터리 {amr['battery']:.1f}%)",
        robot=amr_id,
    )
    return None


def set_nav_to_destination(amr: Dict[str, Any], destination_label: str) -> bool:
    return set_nav_path_for_destination(amr, destination_label)


# ─── 시나리오 격리 실행 ──────────────────────────────────────

import dataclasses
from typing import Optional

class ScenarioAnalytics:
    """시나리오 1회 실행용 격리된 analytics 인스턴스."""

    def __init__(self, sla_threshold_s: float = 120.0) -> None:
        self.sim_time_s: float = 0.0
        self.tasks_started: int = 0
        self.tasks_completed: int = 0
        self.tasks_interrupted: int = 0
        self.tasks_failed: int = 0
        self.total_transport_m: float = 0.0
        self.robot_running_s: Dict[str, float] = {}
        self.robot_idle_s: Dict[str, float] = {}
        self.robot_charge_s: Dict[str, float] = {}
        self.robot_wait_pending_s: Dict[str, float] = {}
        self.task_lead_times: List[float] = []
        self.task_run_times: List[float] = []
        self.task_queue_waits: List[float] = []
        self.charge_count: int = 0
        self.emergency_charge_count: int = 0
        self.sla_threshold_s: float = sla_threshold_s
        self.sla_met_count: int = 0

    # ── 이벤트 기록 ──────────────────────────────────────────

    def record_started(self) -> None:
        self.tasks_started += 1

    def record_completed(self, task: Dict[str, Any]) -> None:
        self.tasks_completed += 1
        try:
            done = task.get("done_sim_s")
            created = task.get("created_sim_s")
            started = task.get("started_sim_s")
            if done is None or created is None:
                return
            lead = done - created
            if 0 <= lead < 86400:
                self.task_lead_times.append(round(lead, 2))
                if lead <= self.sla_threshold_s:
                    self.sla_met_count += 1
            if started is not None:
                run = done - started
                if 0 <= run < 86400:
                    self.task_run_times.append(round(run, 2))
                wait = started - created
                if 0 <= wait < 86400:
                    self.task_queue_waits.append(round(wait, 2))
        except Exception:
            pass

    def record_interrupted(self) -> None:
        self.tasks_interrupted += 1

    def record_failed(self) -> None:
        self.tasks_failed += 1

    def record_charge(self, force: bool = False) -> None:
        self.charge_count += 1
        if force:
            self.emergency_charge_count += 1

    def tick_accum(self, aid: str, status: str, has_pending: bool, dt: float) -> None:
        # sim_time_s는 여기서 누적하지 않음 (_local_tick에서 1회만 누적)
        self.robot_running_s.setdefault(aid, 0.0)
        self.robot_idle_s.setdefault(aid, 0.0)
        self.robot_charge_s.setdefault(aid, 0.0)
        self.robot_wait_pending_s.setdefault(aid, 0.0)
        if status == "running":
            self.robot_running_s[aid] += dt
        elif status == "charging":
            self.robot_charge_s[aid] += dt
        elif status == "idle" and has_pending:
            self.robot_wait_pending_s[aid] += dt
        else:
            self.robot_idle_s[aid] += dt

    def add_transport_m(self, m: float) -> None:
        self.total_transport_m += m

    # ── 요약 생성 ────────────────────────────────────────────

    def _avg(self, lst: List[float]) -> float:
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    def _p95(self, lst: List[float]) -> float:
        if not lst:
            return 0.0
        s = sorted(lst)
        if len(s) < 20:
            return self._avg(s)
        return round(s[int(len(s) * 0.95)], 2)

    def summary(self, amrs: List[Dict[str, Any]], fleet_size: int, duration_s: float) -> Dict[str, Any]:
        t = max(self.sim_time_s, 1e-6)
        n = max(fleet_size, 1)
        capacity = t * n

        run_sum = sum(self.robot_running_s.get(a["id"], 0.0) for a in amrs)
        idle_sum = sum(self.robot_idle_s.get(a["id"], 0.0) for a in amrs)
        charge_sum = sum(self.robot_charge_s.get(a["id"], 0.0) for a in amrs)
        wait_sum = sum(self.robot_wait_pending_s.get(a["id"], 0.0) for a in amrs)

        completed = self.tasks_completed
        started = self.tasks_started
        charge_cnt = self.charge_count
        emg_cnt = self.emergency_charge_count
        error_count = sum(1 for a in amrs if a.get("status") == "error")
        avg_battery = sum(float(a.get("battery", 0)) for a in amrs) / n

        util_run = round(100.0 * run_sum / capacity, 2)
        util_idle = round(100.0 * idle_sum / capacity, 2)
        util_charge = round(100.0 * charge_sum / capacity, 2)
        throughput = round(completed / (duration_s / 3600), 1)

        # 효율 점수: 시간당 처리량 × 가동률 × (1 - 오류율) × SLA달성률
        sla_pct = round(self.sla_met_count / max(completed, 1) * 100.0, 2)
        efficiency_score = round(
            (throughput / max(n, 1))
            * (util_run / 100.0)
            * max(1.0 - error_count / n, 0.0)
            * (sla_pct / 100.0),
            4,
        )

        robot_util_pct = {
            a["id"]: round(100.0 * self.robot_running_s.get(a["id"], 0.0) / t, 2)
            for a in amrs
        }
        robot_charge_pct = {
            a["id"]: round(100.0 * self.robot_charge_s.get(a["id"], 0.0) / t, 2)
            for a in amrs
        }

        return {
            "fleet_size": fleet_size,
            "duration_s": duration_s,
            # 작업
            "tasks_started": started,
            "tasks_completed": completed,
            "tasks_interrupted": self.tasks_interrupted,
            "tasks_failed": self.tasks_failed,
            "task_completion_rate": round(completed / max(started, 1) * 100.0, 2),
            "tasks_per_amr": round(completed / n, 1),
            "throughput_per_hour": throughput,
            # 리드타임
            "avg_lead_time_s": self._avg(self.task_lead_times),
            "avg_run_time_s": self._avg(self.task_run_times),
            "avg_queue_wait_s": self._avg(self.task_queue_waits),
            "p95_lead_time_s": self._p95(self.task_lead_times),
            # SLA
            "sla_threshold_s": self.sla_threshold_s,
            "sla_achievement_pct": sla_pct,
            # 가동률
            "utilization_run_pct": util_run,
            "utilization_idle_pct": util_idle,
            "utilization_charge_pct": util_charge,
            "avg_pending_wait_s": round(wait_sum / n, 2),
            # 충전
            "charge_count": charge_cnt,
            "emergency_charge_count": emg_cnt,
            "emergency_charge_ratio": round(emg_cnt / max(charge_cnt, 1) * 100.0, 2),
            # 이동
            "total_transport_m": round(self.total_transport_m, 2),
            # 종료 상태
            "error_amr_count": error_count,
            "avg_battery_end_pct": round(avg_battery, 1),
            # 개별 로봇
            "robot_utilization_pct": robot_util_pct,
            "robot_charge_pct": robot_charge_pct,
            # 종합 점수
            "efficiency_score": efficiency_score,
        }


def run_scenario_isolated(
    fleet_size: int,
    duration_s: float,
    job_interval_s: float,
    nav_speed: float,
    battery_drain: float,
    charge_rate: float,
    sla_threshold_s: float = 120.0,
) -> Dict[str, Any]:
    import random
    """
    글로벌 analytics/amr_list/task_list를 오염시키지 않는 격리 시나리오 실행.
    실제 simulation.py의 물리/ACS 로직(tick 내부 함수)을 최대한 재사용.
    """
    refresh_station_cache()

    sa = ScenarioAnalytics(sla_threshold_s=sla_threshold_s)

    # ── 로컬 파라미터 (글로벌 sim_params 미변경) ────────────
    lp: Dict[str, Any] = {
        "nav_speed": nav_speed,
        "battery_drain_running": battery_drain,
        "battery_drain_idle": battery_drain * 0.06,
        "battery_drain_running_nopower": battery_drain * 0.38,
        "battery_charge_rate": charge_rate,
        "safety_margin_pct": 10.0,
        "critical_battery_pct": 8.0,
        "yield_stall_reroute_s": 3.5,
        "yield_stall_abort_s": 9.0,
    }

    # ── 플릿 초기화 ─────────────────────────────────────────
    labels = [s for s in STATIONS if s not in CHARGING_STATIONS]
    if not labels:
        labels = list(STATIONS.keys())
    charger_labels = sorted(CHARGING_STATIONS)

    amrs: List[Dict[str, Any]] = []
    for i in range(fleet_size):
        if i % 5 == 4 and charger_labels:
            loc = charger_labels[i % len(charger_labels)]
            bat = 55.0 + (i % 3) * 8
            st = "charging"
        else:
            loc = labels[i % len(labels)]
            bat = 75.0 + (i % 5) * 4
            st = "idle"
        amrs.append(make_amr(f"S{fleet_size}-{i+1:02d}", st, loc, bat))

    tasks: List[Dict[str, Any]] = []
    _job_ctr = [1]
    _local_charger_res: Dict[str, str] = {}

    # ── 헬퍼: 충전기 가용성 (로컬 예약 테이블 기준) ─────────
    def _local_charger_usable(amr_id: str, label: str) -> bool:
        if label not in CHARGING_STATIONS:
            return False
        res = _local_charger_res.get(label)
        if res and res != amr_id:
            return False
        for a in amrs:
            if a["id"] != amr_id and a.get("location") == label and a.get("status") == "charging":
                return False
        for t in tasks:
            if t.get("destination") != label:
                continue
            oid = str(t.get("amr_id") or "")
            if oid and oid != amr_id and t.get("status") in ("pending", "running"):
                return False
        return True

    def _local_nearest_free_charger(amr_id: str, x: float, y: float) -> Optional[str]:
        cfg = get_config()
        scored: List[Tuple[float, str]] = []
        for name in sorted(CHARGING_STATIONS):
            if name not in STATIONS:
                continue
            if not _local_charger_usable(amr_id, name):
                continue
            gd = cfg.graph_distance_m(x, y, name)
            if gd is not None:
                scored.append((gd, name))
        if not scored:
            return None
        scored.sort()
        return scored[0][1]

    def _local_evaluate_budget(a: Dict[str, Any], pickup: str, drop: str) -> Tuple[bool, Optional[str], float, float]:
        bat = float(a.get("battery") or 0.0)
        margin = float(lp["safety_margin_pct"])
        ch = _local_nearest_free_charger(a["id"], float(a["x"]), float(a["y"]))
        if not ch:
            return False, None, 0.0, bat
        dm = graph_chain_distance_m(float(a["x"]), float(a["y"]), [pickup, drop, ch])
        if dm is None:
            return False, ch, 0.0, bat
        # 로컬 파라미터 기반 소모율
        cost_per_m = lp["battery_drain_running"] / max(lp["nav_speed"], 0.01)
        cost_pct = dm * cost_per_m
        after = bat - cost_pct
        if cost_pct > bat or after < margin:
            return False, ch, cost_pct, after
        return True, ch, cost_pct, after

    def _local_charge_leg_ok(a: Dict[str, Any], charger: str) -> Tuple[bool, float, float]:
        bat = float(a.get("battery") or 0.0)
        dm = graph_chain_distance_m(float(a["x"]), float(a["y"]), [charger])
        if dm is None:
            return False, 0.0, bat
        cost_per_m = lp["battery_drain_running"] / max(lp["nav_speed"], 0.01)
        cost_pct = dm * cost_per_m
        after = bat - cost_pct
        return (cost_pct < bat), cost_pct, after

    # ── 충전 디스패치 ────────────────────────────────────────
    def _begin_charge(a: Dict[str, Any], force: bool = False) -> bool:
        aid = a["id"]
        # 기존 예약 해제
        for k in [k for k, v in _local_charger_res.items() if v == aid]:
            del _local_charger_res[k]

        cfg = get_config()
        scored: List[Tuple[float, str]] = []
        for name in sorted(CHARGING_STATIONS):
            if name not in STATIONS:
                continue
            if not _local_charger_usable(aid, name):
                continue
            gd = cfg.graph_distance_m(float(a["x"]), float(a["y"]), name)
            if gd is not None:
                scored.append((gd, name))
        scored.sort()

        for _, ch in scored:
            ok, _, _ = _local_charge_leg_ok(a, ch)
            if not ok and not force:
                continue
            _local_charger_res[ch] = aid
            tid = f"CHG-{_job_ctr[0]:04d}"
            _job_ctr[0] += 1
            ct: Dict[str, Any] = {
                "task_id": tid, "amr_id": aid,
                "task_type": "charge", "destination": ch,
                "priority": 99, "status": "running",
                "created_at": datetime.now().isoformat(),
                "started_at": datetime.now().isoformat(),
                "done_at": None, "origin": str(a.get("location", "")),
                "progress_pct": 0.0, "path_total_m": 0.0, "auto_generated": True,
                "created_sim_s": sa.sim_time_s,   # ← 추가
                "started_sim_s": sa.sim_time_s,   # 충전은 생성 즉시 시작
                "done_sim_s": None,
            }
            tasks.append(ct)
            a["status"] = "running"
            a["location"] = ch
            a["active_task_id"] = tid
            a["yield_accum_s"] = 0.0
            a["reroute_tried"] = False
            if not set_nav_path_for_destination(a, ch):
                tasks.pop()
                if _local_charger_res.get(ch) == aid:
                    del _local_charger_res[ch]
                a["status"] = "idle"
                a["location"] = str(a.get("location", ""))
                a["active_task_id"] = None
                clear_nav(a)
                snap_amr_to_location(a)
                continue
            ct["path_total_m"] = path_polyline_total_m(a)
            sa.record_started()
            sa.record_charge(force=force)
            return True
        return False

    # ── 작업 자동시작 ────────────────────────────────────────
    def _try_autostart(a: Dict[str, Any]) -> None:
        if a["status"] != "idle":
            return
        pending = [t for t in tasks if t.get("amr_id") == a["id"] and t.get("status") == "pending"]
        if not pending:
            return
        pending.sort(key=lambda t: (-int(t.get("priority", 1)), str(t.get("created_at", ""))))
        for t in pending:
            dest = str(t.get("destination") or "")
            ok, _, cost, _ = _local_evaluate_budget(a, str(a.get("location", "")), dest)
            if not ok:
                _begin_charge(a, force=False) or _begin_charge(a, force=True)
                return
            a["status"] = "running"
            a["location"] = dest
            a["active_task_id"] = t["task_id"]
            a["yield_accum_s"] = 0.0
            a["reroute_tried"] = False
            if not set_nav_path_for_destination(a, dest):
                a["status"] = "idle"
                a["active_task_id"] = None
                continue
            t["status"] = "running"
            t["started_sim_s"] = sa.sim_time_s  # ← 추가 (기존 started_at 줄 아래)
            t["started_at"] = datetime.now().isoformat()
            t["path_total_m"] = path_polyline_total_m(a)
            sa.record_started()
            return

    # ── 작업 완료 처리 ───────────────────────────────────────
    def _complete_task_local(task: Dict[str, Any], a: Dict[str, Any]) -> None:
        task["status"] = "done"
        task["done_sim_s"] = sa.sim_time_s  # ← 추가
        task["done_at"] = datetime.now().isoformat()
        task["progress_pct"] = 100.0
        sa.record_completed(task)
        dest = str(task.get("destination") or "")
        a["active_task_id"] = None
        a["location"] = dest
        clear_nav(a)
        snap_amr_to_location(a)
        if dest in CHARGING_STATIONS:
            if _local_charger_res.get(dest) == a["id"]:
                del _local_charger_res[dest]
            a["status"] = "charging"
            a["low_bat_flag"] = False
            a["charge_after_job"] = False
        else:
            a["status"] = "idle"
            need_charge = (
                float(a["battery"]) < CHARGE_TRIGGER_PCT
                or a.get("charge_after_job")
                or a.get("low_bat_flag")
            )
            a["charge_after_job"] = False
            if need_charge:
                _begin_charge(a, force=True)
            else:
                _try_autostart(a)

    # ── 작업 생성 (디스패처) ─────────────────────────────────
    def _spawn_job() -> None:
        dests = [s for s in STATIONS if s not in CHARGING_STATIONS]
        if not dests:
            return
        destination = random.choice(dests)
        candidates: List[Tuple[float, Dict[str, Any]]] = []
        cfg = get_config()
        for a in amrs:
            ensure_amr_shape(a)
            if a["status"] != "idle":
                continue
            if float(a["battery"]) <= LOW_BATTERY_FORCE_CHARGE_PCT:
                _begin_charge(a, force=True)
                continue
            if any(t.get("amr_id") == a["id"] and t.get("status") in ("pending", "running") for t in tasks):
                continue
            if station_claimed_by_other(destination, amrs, tasks, a["id"]):
                continue
            ok, _, cost, _ = _local_evaluate_budget(a, str(a.get("location", "")), destination)
            if ok:
                gd = cfg.graph_distance_m(float(a["x"]), float(a["y"]), destination) or 0.0
                candidates.append((gd, a))
        if not candidates:
            return
        candidates.sort(key=lambda x: x[0])
        _, amr = candidates[0]
        tid = f"SC-{fleet_size}-{_job_ctr[0]:04d}"
        _job_ctr[0] += 1
        t: Dict[str, Any] = {
            "task_id": tid, "amr_id": amr["id"],
            "task_type": "move", "destination": destination,
            "pickup_station": str(amr.get("location", "")),
            "drop_station": destination,
            "priority": random.randint(1, 5), "status": "pending",
            "created_at": datetime.now().isoformat(),
            "started_at": None, "done_at": None,
            "origin": str(amr.get("location", "")),
            "progress_pct": 0.0, "path_total_m": 0.0, "auto_generated": True,
            "created_sim_s": sa.sim_time_s,  # ← 추가
            "started_sim_s": None,
            "done_sim_s": None,
        }
        tasks.append(t)
        # 즉시 시작 시도
        dest = str(t.get("destination") or "")
        ok, _, _, _ = _local_evaluate_budget(amr, str(amr.get("location", "")), dest)
        if ok and amr["status"] == "idle":
            prev_loc = str(amr.get("location", ""))
            amr["status"] = "running"
            amr["location"] = dest
            amr["active_task_id"] = tid
            amr["yield_accum_s"] = 0.0
            amr["reroute_tried"] = False
            if set_nav_path_for_destination(amr, dest):
                t["status"] = "running"
                t["started_at"] = datetime.now().isoformat()
                t["started_sim_s"] = sa.sim_time_s  # ← 이거 추가
                t["path_total_m"] = path_polyline_total_m(amr)
                sa.record_started()
            else:
                amr["status"] = "idle"
                amr["location"] = prev_loc
                amr["active_task_id"] = None
                clear_nav(amr)

    # ── 로컬 틱 (실제 시뮬 물리 재사용) ─────────────────────
    def _local_tick(dt: float) -> None:
        sa.sim_time_s += dt  # ← 틱당 1회만
        nav = float(lp["nav_speed"])
        dr_run = float(lp["battery_drain_running"])
        dr_idle = float(lp["battery_drain_idle"])
        dr_nop = float(lp["battery_drain_running_nopower"])
        chg = float(lp["battery_charge_rate"])
        rr_s = float(lp["yield_stall_reroute_s"])
        ab_s = float(lp["yield_stall_abort_s"])
        crit = float(lp["critical_battery_pct"])
        cost_per_m = dr_run / max(nav, 0.01)

        # 교행 충돌 감지
        running = [a for a in amrs if a["status"] == "running" and a.get("path_queue")]
        yield_ids: Set[str] = set()
        by_wp: Dict[Tuple[int, int], List[str]] = {}
        for a in running:
            w = a["path_queue"][0]
            key = (round(float(w[0]) * 50), round(float(w[1]) * 50))
            by_wp.setdefault(key, []).append(a["id"])
        for aids in by_wp.values():
            if len(aids) >= 2:
                aids.sort()
                yield_ids.update(aids[1:])
        for i in range(len(running)):
            for j in range(i + 1, len(running)):
                a1, a2 = running[i], running[j]
                d = distance_m(float(a1["x"]), float(a1["y"]), float(a2["x"]), float(a2["y"]))
                if d < CONFLICT_DIST_M:
                    yield_ids.add(sorted([a1["id"], a2["id"]])[1])
        for a in amrs:
            a["acs_yield"] = a["id"] in yield_ids

        for a in amrs:
            ensure_amr_shape(a)
            aid = a["id"]
            st = a["status"]
            if st == "error":
                continue

            has_pending = any(t.get("amr_id") == aid and t.get("status") == "pending" for t in tasks)
            sa.tick_accum(aid, st, has_pending, dt)

            b = float(a["battery"])

            if b <= 0.0:
                a["battery"] = 0.0
                a["status"] = "error"
                clear_nav(a)
                tid = a.get("active_task_id")
                if tid:
                    task = next((t for t in tasks if t.get("task_id") == tid), None)
                    if task and task.get("status") == "running":
                        task["status"] = "failed"
                        task["done_at"] = datetime.now().isoformat()
                        sa.record_failed()
                a["active_task_id"] = None
                for k in [k for k, v in _local_charger_res.items() if v == aid]:
                    del _local_charger_res[k]
                continue

            if st == "running":
                tid = a.get("active_task_id")
                task = next((t for t in tasks if t.get("task_id") == tid), None) if tid else None

                # 비상 임계 배터리
                if task and task.get("status") == "running" and task.get("task_type") not in ("charge", "emergency_drop"):
                    if b <= crit:
                        task["status"] = "interrupted"
                        task["done_at"] = datetime.now().isoformat()
                        task["interrupt_reason"] = "critical_battery"
                        sa.record_interrupted()
                        a["active_task_id"] = None
                        clear_nav(a)
                        _begin_charge(a, force=True)
                        b = max(0.0, b - dr_nop * dt)
                        a["battery"] = round(min(100.0, b), 2)
                        continue

                # 교행 정체 처리
                if task and task.get("status") == "running" and a.get("acs_yield"):
                    a["yield_accum_s"] = float(a.get("yield_accum_s") or 0.0) + dt
                    ya = float(a.get("yield_accum_s") or 0.0)
                    if ya >= rr_s and not a.get("reroute_tried"):
                        dest_t = str(task.get("destination") or "")
                        cfg = get_config()
                        pq = a.get("path_queue") or []
                        if pq:
                            ax, ay = float(pq[0][0]), float(pq[0][1])
                            avoid_nid = cfg.nearest_node_id(ax, ay)
                            if avoid_nid:
                                alt = cfg.path_for_labels_avoid_nodes(float(a["x"]), float(a["y"]), dest_t, {avoid_nid})
                                if alt:
                                    a["path_queue"] = [[float(px), float(py)] for px, py in alt]
                                    a["yield_accum_s"] = 0.0
                                    task["path_total_m"] = path_polyline_total_m(a)
                        a["reroute_tried"] = True
                    rem_cost = path_polyline_remaining_m(a) * cost_per_m
                    if ya >= ab_s or (ya >= 1.0 and rem_cost > max(b - float(lp["safety_margin_pct"]), 0.0)):
                        if task.get("status") == "running":
                            task["status"] = "interrupted"
                            task["done_at"] = datetime.now().isoformat()
                            task["interrupt_reason"] = "yield_stall_abort"
                            sa.record_interrupted()
                        a["active_task_id"] = None
                        clear_nav(a)
                        a["status"] = "idle"
                        _begin_charge(a, force=True)
                        b = max(0.0, b - dr_nop * dt)
                        a["battery"] = round(min(100.0, b), 2)
                        continue
                elif task and task.get("status") == "running":
                    a["yield_accum_s"] = 0.0
                    a["reroute_tried"] = False

                if b < CHARGE_TRIGGER_PCT:
                    a["low_bat_flag"] = True
                    a["charge_after_job"] = True

                # 이동 물리
                if not a.get("acs_yield"):
                    target = _current_nav_target(a)
                    if target is not None:
                        tx, ty = target
                        x, y = float(a["x"]), float(a["y"])
                        dx, dy = tx - x, ty - y
                        dist = math.hypot(dx, dy)
                        if dist < ARRIVAL_EPS:
                            sa.add_transport_m(dist)
                            a["x"], a["y"] = tx, ty
                            a["vx"] = a["vy"] = 0.0
                            _advance_path(a)
                        else:
                            step = min(nav * dt, dist)
                            ux, uy = dx / dist, dy / dist
                            sa.add_transport_m(step)
                            a["x"] = x + ux * step
                            a["y"] = y + uy * step
                            a["vx"] = ux * nav
                            a["vy"] = uy * nav
                            a["yaw"] = math.atan2(dy, dx)
                        b = max(0.0, b - dr_run * dt)
                    else:
                        a["vx"] = a["vy"] = 0.0
                        b = max(0.0, b - dr_nop * dt)
                else:
                    a["vx"] = a["vy"] = 0.0
                    b = max(0.0, b - dr_nop * dt)

                # 진행률 업데이트
                if task and task.get("status") == "running":
                    _update_task_progress(task, a)

            elif st == "charging":
                a["vx"] = a["vy"] = 0.0
                loc = a.get("location") or ""
                if loc in CHARGING_STATIONS:
                    cx, cy = station_coords(loc)
                    a["x"], a["y"] = cx, cy
                b = min(100.0, b + chg * dt)
                if b >= CHARGE_COMPLETE_PCT:
                    a["status"] = "idle"
                    a["low_bat_flag"] = False
                    _try_autostart(a)
            else:  # idle
                a["vx"] = a["vy"] = 0.0
                if b < CHARGE_TRIGGER_PCT and not a.get("active_task_id"):
                    already = any(
                        t.get("amr_id") == aid and t.get("task_type") == "charge"
                        and t.get("status") in ("pending", "running")
                        for t in tasks
                    )
                    if not already:
                        _begin_charge(a, force=True)
                b = max(0.0, b - dr_idle * dt)

            a["battery"] = round(max(0.0, min(100.0, b)), 2)

        # 도착 처리
        for a in amrs:
            ensure_amr_shape(a)
            if a["status"] != "running":
                continue
            tid = a.get("active_task_id")
            if not tid:
                continue
            task = next((t for t in tasks if t.get("task_id") == tid), None)
            if not task or task.get("status") != "running":
                continue
            if a.get("path_queue"):
                continue
            dest = str(task.get("destination") or "")
            tx, ty = station_coords(dest)
            if distance_m(float(a["x"]), float(a["y"]), tx, ty) > ARRIVAL_EPS * 3:
                continue
            _complete_task_local(task, a)

    # ── 실제 시뮬 루프 ──────────────────────────────────────
    hz = 50.0
    dt = 1.0 / hz
    steps = int(duration_s * hz)
    dispatch_accum = 0.0

    for _ in range(steps):
        _local_tick(dt)
        dispatch_accum += dt
        if dispatch_accum >= job_interval_s:
            dispatch_accum = 0.0
            _spawn_job()

    return sa.summary(amrs, fleet_size, duration_s)