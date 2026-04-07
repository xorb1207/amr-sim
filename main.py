from __future__ import annotations

import asyncio
import json
import os
import random
import time as time_module
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from starlette.websockets import WebSocketState

import wave_map as wave_map_mod

from simulation import (
    get_battery_cost_per_meter,
    CHARGING_STATIONS,
    LOW_BATTERY_FORCE_CHARGE_PCT,
    MIN_BATTERY_AFTER_TASK_PCT,
    STATIONS,
    _begin_charge_dispatch,
    acs_log_event,
    acs_log_snapshot,
    build_active_jobs,
    build_analytics_summary,
    build_fleet_states,
    build_station_overlay,
    clear_nav,
    compute_acs_state,
    ensure_amr_shape,
    evaluate_charge_leg_budget,
    evaluate_transport_energy_budget,
    make_amr,
    nearest_charging_station,
    record_task_completed,
    record_task_interrupted,
    record_task_failed,
    record_charge_event,
    refresh_station_cache,
    rescue_warp_amr,
    set_nav_path_for_destination,
    sim_params,
    snap_amr_to_location,
    start_task_for_amr,
    station_claimed_by_other,
    tick,
    try_autostart_pending,
    run_scenario_isolated,
)

# ─── 설정 (폐쇄망·Docker: 환경변수로 조정) ─────────────────
SIM_HZ = float(os.getenv("SIM_HZ", "20"))
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
)
_cors_list = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
_allow_all = os.getenv("CORS_ALLOW_ALL", "").lower() in ("1", "true", "yes")
if _allow_all:
    _cors_list = ["*"]

_cors_credentials = _cors_list != ["*"]

MAP_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.json")
wave_map_mod.load_map_file(MAP_JSON_PATH)
refresh_station_cache()

from simulation import _flog as _sim_flog
_sim_flog("info", f"{'='*60}")
_sim_flog("info", f"서버 시작 | map={MAP_JSON_PATH}")
_sim_flog("info", f"충전기: {sorted(CHARGING_STATIONS)} | 스테이션: {sorted(STATIONS.keys())}")

# AMR + 작업 (시뮬레이터와 동기화)
amr_list: List[Dict[str, Any]] = [
    make_amr("AMR-001", "idle", "Station-A", 95.0),
    make_amr("AMR-002", "idle", "Station-C", 72.0),
    make_amr("AMR-003", "charging", "Charger-1", 30.0),
]
task_list: List[Dict[str, Any]] = []
task_counter = 1


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        """accept() 이후 등록만 수행 (Upgrade 핸들링 단순화)."""
        async with self._lock:
            self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast_json(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            dead: List[WebSocket] = []
            text = json.dumps(payload, ensure_ascii=False)
            for ws in self._connections:
                if ws.client_state != WebSocketState.CONNECTED:
                    dead.append(ws)
                    continue
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._connections.discard(ws)


manager = ConnectionManager()
_sim_task: Optional[asyncio.Task] = None
_dispatcher_accum = 0.0


def _build_ws_payload() -> Dict[str, Any]:
    for a in amr_list:
        ensure_amr_shape(a)
    fleet = build_fleet_states(amr_list, task_list)
    cfg = wave_map_mod.get_config()
    return {
        "type": "fleet_tick",
        "t_mono": time_module.monotonic(),
        "fleet_states": fleet,
        "amrs": [dict(a) for a in amr_list],
        "tasks": [dict(t) for t in task_list],
        "active_jobs": build_active_jobs(task_list, amr_list),
        "acs_logs": acs_log_snapshot(),
        "station_overlay": build_station_overlay(amr_list, task_list),
        "wave_map": cfg.to_public_dict(),
        "sim_params": dict(sim_params),
        "analytics": build_analytics_summary(amr_list),
    }


def spawn_auto_transport() -> None:
    """Idle·배터리·그래프 거리 기준으로 가상 운반 작업을 생성하고 즉시 시작 시도."""
    global task_counter
    refresh_station_cache()
    dests = [s for s in STATIONS if s not in CHARGING_STATIONS]
    if len(dests) < 1:
        return
    destination = random.choice(dests)
    cfg = wave_map_mod.get_config()
    candidates: List[tuple[float, Dict[str, Any]]] = []
    for a in amr_list:
        ensure_amr_shape(a)
        if a["status"] != "idle":
            continue
        if compute_acs_state(a) != "Idle":
            continue
        if float(a["battery"]) <= 0:
            continue
        if any(
            t.get("amr_id") == a["id"] and t.get("status") in ("pending", "running")
            for t in task_list
        ):
            continue
        pickup = str(a.get("location") or "")
        ok_b, plan_ch, cost_pct, _after = evaluate_transport_energy_budget(
            a, pickup, destination, amr_list, task_list
        )
        if not ok_b:
            acs_log_event(
                "dispatch_reject",
                f"{a['id']} 자동작업 에너지 예산 거부 (~{cost_pct:.1f}%) → 충전",
                robot=a["id"],
            )
            if not _begin_charge_dispatch(a, plan_ch, amr_list, task_list, force=False):
                _begin_charge_dispatch(a, plan_ch, amr_list, task_list, force=True)
            continue
        gd = cfg.graph_distance_m(float(a["x"]), float(a["y"]), destination)
        if gd is None:
            continue
        if station_claimed_by_other(destination, amr_list, task_list, a["id"]):
            continue
        candidates.append((gd, a, cost_pct))
    if not candidates:
        return
    candidates.sort(key=lambda x: x[0])
    gd_pick, amr, budget_cost = candidates[0]
    new_task: Dict[str, Any] = {
        "task_id": f"TASK-{task_counter:03d}",
        "amr_id": amr["id"],
        "task_type": "move",
        "destination": destination,
        "pickup_station": str(amr.get("location") or ""),
        "drop_station": destination,
        "priority": random.randint(1, 5),
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "done_at": None,
        "origin": str(amr.get("location") or ""),
        "progress_pct": 0.0,
        "path_total_m": 0.0,
        "auto_generated": True,
        "estimated_battery_cost_pct": round(budget_cost, 2),
    }
    task_list.append(new_task)
    task_counter += 1
    acs_log_event(
        "auto_dispatch",
        f"자동 작업 생성 · {new_task['task_id']} → {destination} ({amr['id']})",
        job_id=new_task["task_id"],
        robot=amr["id"],
    )
    start_task_for_amr(new_task, amr, amr_list, task_list)


def dispatcher_tick(dt: float) -> None:
    global _dispatcher_accum
    try:
        enabled = float(sim_params.get("auto_dispatch_enabled", 1.0))
    except (TypeError, ValueError):
        enabled = 0.0
    if enabled <= 0:
        return
    try:
        iv = float(sim_params.get("auto_job_interval_s", 7.0))
    except (TypeError, ValueError):
        iv = 7.0
    iv = max(2.0, min(120.0, iv))
    _dispatcher_accum += dt
    if _dispatcher_accum < iv:
        return
    _dispatcher_accum = 0.0
    spawn_auto_transport()


async def _simulation_loop() -> None:
    interval = 1.0 / max(SIM_HZ, 1.0)
    while True:
        tick(amr_list, interval, task_list)
        dispatcher_tick(interval)
        await manager.broadcast_json(_build_ws_payload())
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sim_task
    _sim_task = asyncio.create_task(_simulation_loop())
    yield
    if _sim_task:
        _sim_task.cancel()
        try:
            await _sim_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="FAB AMR ICS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_list,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskCreate(BaseModel):
    amr_id: str = Field(..., min_length=1)
    task_type: str = Field("move", min_length=1)
    destination: str = Field(..., min_length=1)
    priority: int = Field(1, ge=1, le=99)
    pickup_station: Optional[str] = None
    drop_station: Optional[str] = None

    @field_validator("amr_id", "task_type", "destination", "pickup_station", "drop_station", mode="before")
    @classmethod
    def strip_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class RescueWarpBody(BaseModel):
    charger_label: Optional[str] = None
    min_battery_floor: float = Field(14.0, ge=5.0, le=40.0)


class StatusUpdate(BaseModel):
    status: str = Field(..., min_length=1)

    @field_validator("status", mode="before")
    @classmethod
    def strip_status(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class SimParamsUpdate(BaseModel):
    nav_speed: Optional[float] = None
    battery_drain_running: Optional[float] = None
    battery_drain_idle: Optional[float] = None
    battery_drain_running_nopower: Optional[float] = None
    battery_charge_rate: Optional[float] = None
    auto_dispatch_enabled: Optional[float] = Field(None, ge=0, le=1)
    auto_job_interval_s: Optional[float] = Field(None, ge=2, le=120)
    safety_margin_pct: Optional[float] = Field(None, ge=3, le=25)
    critical_battery_pct: Optional[float] = Field(None, ge=3, le=15)
    yield_stall_reroute_s: Optional[float] = Field(None, ge=0.5, le=30)
    yield_stall_abort_s: Optional[float] = Field(None, ge=2, le=60)
    fleet_size: Optional[int] = Field(None, ge=1, le=12)


def _rebuild_fleet(size: int) -> None:
    global amr_list, task_list, task_counter
    refresh_station_cache()
    labels = list(STATIONS.keys())
    if not labels:
        labels = ["Station-A"]
    task_list.clear()
    task_counter = 1
    amr_list.clear()
    for i in range(size):
        loc = labels[i % len(labels)]
        st = "charging" if i % 4 == 3 and any("Charge" in x for x in labels) else "idle"
        if st == "charging":
            cl = next((x for x in labels if "Charge" in x), labels[0])
            amr_list.append(
                make_amr(f"AMR-{i + 1:03d}", "charging", cl, 55.0 + (i % 3) * 10),
            )
        else:
            amr_list.append(
                make_amr(f"AMR-{i + 1:03d}", "idle", loc, 88.0 - (i % 5) * 3),
            )


# ─── AMR API ─────────────────────────────────────────────

@app.get("/")
def root():
    return {"message": "FAB AMR 시스템 가동 중", "time": datetime.now().isoformat()}


@app.get("/map/stations")
def map_stations():
    """맵·점유·충전소·예측 계수 (프론트 사전 계산용)."""
    refresh_station_cache()
    for a in amr_list:
        ensure_amr_shape(a)
    return {
        "map_name": "fab_l1",
        "stations": STATIONS,
        "charging_station_ids": sorted(CHARGING_STATIONS),
        "battery_cost_per_meter": get_battery_cost_per_meter(),
        "min_battery_after_task_pct": MIN_BATTERY_AFTER_TASK_PCT,
        "low_battery_charge_pct": LOW_BATTERY_FORCE_CHARGE_PCT,
        "station_overlay": build_station_overlay(amr_list, task_list),
        "wave_map": wave_map_mod.get_config().to_public_dict(),
    }


@app.get("/map/editor")
def get_map_editor():
    return wave_map_mod.get_config().to_public_dict()


@app.post("/map/editor/save")
def save_map_editor(body: Dict[str, Any] = Body(...)):
    try:
        wave_map_mod.save_map_file(MAP_JSON_PATH, body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    refresh_station_cache()
    for a in amr_list:
        ensure_amr_shape(a)
        if a.get("status") == "running":
            dest = str(a.get("location") or "")
            tid = a.get("active_task_id")
            task = next((t for t in task_list if t.get("task_id") == tid), None)
            if task and task.get("status") == "running":
                dest = str(task.get("destination") or dest)
            set_nav_path_for_destination(a, dest)
        else:
            snap_amr_to_location(a)
    return {"message": "맵이 map.json에 저장되었고 시뮬레이터에 반영되었습니다."}


@app.get("/sim/params")
def get_sim_params():
    return {"params": sim_params, "fleet_size": len(amr_list)}


@app.patch("/sim/params")
def patch_sim_params(body: SimParamsUpdate):
    data = body.model_dump(exclude_unset=True)
    if "fleet_size" in data:
        _rebuild_fleet(int(data.pop("fleet_size")))
    for k, v in data.items():
        if v is not None and k in sim_params:
            sim_params[k] = float(v)
    return {"params": sim_params, "fleet_size": len(amr_list)}


@app.get("/analytics/summary")
def get_analytics_summary():
    for a in amr_list:
        ensure_amr_shape(a)
    return build_analytics_summary(amr_list)


@app.get("/routing/preview")
def routing_preview(amr_id: str, destination: str):
    amr = next((a for a in amr_list if a["id"] == amr_id), None)
    if not amr:
        raise HTTPException(status_code=404, detail="AMR 없음")
    ensure_amr_shape(amr)
    cfg = wave_map_mod.get_config()
    dist = cfg.graph_distance_m(float(amr["x"]), float(amr["y"]), destination)
    if dist is None:
        raise HTTPException(status_code=400, detail="경로를 찾을 수 없습니다")
    return {
        "distance_m": round(dist, 3),
        "estimated_battery_pct": round(dist * get_battery_cost_per_meter(), 2),
    }


@app.get("/analytics/report.txt")
def analytics_report_text():
    for a in amr_list:
        ensure_amr_shape(a)
    s = build_analytics_summary(amr_list)
    lines = [
        "WAVE Fleet Planner — Analysis Report",
        f"Generated: {datetime.now().isoformat()}",
        "",
        "=== 플릿 기본 현황 ===",
        f"시뮬레이션 시간 (s):       {s['sim_time_s']}",
        f"플릿 크기:                 {s['fleet_size']}",
        f"시작 작업 수:              {s['tasks_started']}",
        f"완료 작업 수:              {s['tasks_completed']}",
        f"중단 작업 수:              {s.get('tasks_interrupted', 0)}",
        f"실패 작업 수:              {s.get('tasks_failed', 0)}",
        f"작업 완료율 (%):           {s.get('task_completion_rate', 0)}",
        f"누적 운송 거리 (m):        {s['total_transport_m']}",
        "",
        "=== 태스크 리드타임 ===",
        f"평균 리드타임 (s):         {s.get('avg_lead_time_s', 0)}",
        f"평균 실행시간 (s):         {s.get('avg_run_time_s', 0)}",
        f"평균 큐 대기 (s):          {s.get('avg_queue_wait_s', 0)}",
        f"P95 리드타임 (s):          {s.get('p95_lead_time_s', 0)}",
        f"SLA 기준 (s):              {s.get('sla_threshold_s', 120)}",
        f"SLA 달성률 (%):            {s.get('sla_achievement_pct', 0)}",
        "",
        "=== 가동률 분해 ===",
        f"운행 가동률 (%):           {s['utilization_run_pct']}",
        f"유휴 가동률 (%):           {s['utilization_idle_pct']}",
        f"충전 가동률 (%):           {s.get('utilization_charge_pct', 0)}",
        f"평균 대기열 대기 (s/대):    {s['avg_pending_wait_s']}",
        "",
        "=== 충전 전략 ===",
        f"총 충전 횟수:              {s.get('charge_count', 0)}",
        f"긴급 충전 횟수:            {s.get('emergency_charge_count', 0)}",
        f"긴급 충전 비율 (%):        {s.get('emergency_charge_ratio', 0)}",
        "",
        "=== 개별 로봇 가동률 ===",
        *[f"  {rid}: 운행 {pct}% | 충전 {s.get('robot_charge_pct', {}).get(rid, 0)}%"
          for rid, pct in s.get("robot_utilization_pct", {}).items()],
        "",
        "=== 운영 파라미터 ===",
        *[f"  {k}: {v}" for k, v in s.get("sim_params", {}).items()],
    ]
    return "\n".join(lines)


@app.get("/fleet_states")
def get_fleet_states():
    for a in amr_list:
        ensure_amr_shape(a)
    return build_fleet_states(amr_list, task_list)


@app.get("/amrs")
def get_amrs():
    for a in amr_list:
        ensure_amr_shape(a)
    return {"total": len(amr_list), "amrs": amr_list}


@app.get("/amrs/{amr_id}")
def get_amr(amr_id: str):
    amr = next((a for a in amr_list if a["id"] == amr_id), None)
    if not amr:
        return {"error": f"{amr_id}를 찾을 수 없습니다"}
    ensure_amr_shape(amr)
    return amr


@app.post("/amrs/{amr_id}/rescue-warp")
def post_rescue_warp(amr_id: str, body: RescueWarpBody = RescueWarpBody()):
    err = rescue_warp_amr(
        amr_id,
        (body.charger_label or "").strip() or None,
        amr_list,
        task_list,
        float(body.min_battery_floor),
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    amr = next((a for a in amr_list if a["id"] == amr_id), None)
    if amr:
        ensure_amr_shape(amr)
    return {"message": f"{amr_id} 구조 워프 완료 (충전 시작)", "amr": amr}


@app.patch("/amrs/{amr_id}/status")
def update_amr_status(amr_id: str, body: StatusUpdate):
    amr = next((a for a in amr_list if a["id"] == amr_id), None)
    if not amr:
        return {"error": f"{amr_id}를 찾을 수 없습니다"}
    ensure_amr_shape(amr)

    allowed = {
        "idle": ["running", "charging"],
        "running": ["idle", "charging"],
        "charging": ["idle"],
    }
    cur = str(amr.get("status") or "idle")
    if body.status not in allowed.get(cur, []):
        return {"error": f"{cur} → {body.status} 전환 불가"}

    amr["status"] = body.status
    if body.status == "charging":
        amr["location"] = "Charger-1"
        snap_amr_to_location(amr)
        clear_nav(amr)
    elif body.status == "idle":
        clear_nav(amr)
        snap_amr_to_location(amr)

    return {"message": f"{amr_id} 상태 변경 완료", "amr": amr}


@app.websocket("/ws/fleet")
async def websocket_fleet(ws: WebSocket):
    await ws.accept()
    await manager.register(ws)
    try:
        if ws.client_state == WebSocketState.CONNECTED:
            await ws.send_json(_build_ws_payload())
        while ws.client_state == WebSocketState.CONNECTED:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)


# ─── Task API ──────────────────────────────────────────────

@app.get("/tasks")
def get_tasks():
    return {"total": len(task_list), "tasks": task_list}


@app.post("/tasks")
def create_task(task: TaskCreate):
    global task_counter

    destination = task.destination
    if destination not in STATIONS:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 목적지입니다: {destination}",
        )

    amr = next((a for a in amr_list if a["id"] == task.amr_id), None)
    if not amr:
        raise HTTPException(status_code=400, detail=f"{task.amr_id}를 찾을 수 없습니다")
    ensure_amr_shape(amr)

    if amr["status"] != "idle":
        raise HTTPException(
            status_code=400,
            detail=f"{task.amr_id}는 {amr['status']} 상태라 작업 배정 불가",
        )

    blocker = station_claimed_by_other(destination, amr_list, task_list, task.amr_id)
    if blocker:
        raise HTTPException(
            status_code=400,
            detail=f"스테이션 '{destination}'은(는) {blocker}에 의해 점유·예약되어 있습니다.",
        )

    bat = float(amr.get("battery") or 0.0)
    if bat <= LOW_BATTERY_FORCE_CHARGE_PCT and destination not in CHARGING_STATIONS:
        suggest = nearest_charging_station(float(amr["x"]), float(amr["y"]))
        raise HTTPException(
            status_code=400,
            detail=(
                f"배터리가 {LOW_BATTERY_FORCE_CHARGE_PCT}% 이하입니다. "
                f"충전 스테이션(권장: {suggest})으로의 이동 작업만 등록할 수 있습니다."
            ),
        )

    pickup = (task.pickup_station or "").strip() or str(amr.get("location") or "")
    drop = (task.drop_station or "").strip() or destination
    if pickup not in STATIONS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 상차지: {pickup}")
    if drop not in STATIONS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 하차지: {drop}")

    if destination in CHARGING_STATIONS:
        ok_leg, cost_pct, after = evaluate_charge_leg_budget(
            amr, destination, amr_list, task_list
        )
        if not ok_leg:
            _begin_charge_dispatch(amr, destination, amr_list, task_list, force=True)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"충전기까지 에너지 예산 부족 (예상 소모 ~{cost_pct:.1f}%, 잔량 {after:.1f}%). "
                    "긴급 충전 미션을 발행했습니다."
                ),
            )
        est_cost = cost_pct
    else:
        ok_b, plan_ch, cost_pct, after = evaluate_transport_energy_budget(
            amr, pickup, drop, amr_list, task_list
        )
        if not ok_b:
            if plan_ch:
                if not _begin_charge_dispatch(amr, plan_ch, amr_list, task_list, force=False):
                    _begin_charge_dispatch(amr, plan_ch, amr_list, task_list, force=True)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"에너지 예산 거부: 현재→상차→하차→충전 전체 ~{cost_pct:.1f}% 예상, "
                    f"완료 후 잔량 {after:.1f}% (안전 마진 미달). 가용 시 충전 미션을 발행했습니다."
                ),
            )
        est_cost = cost_pct

    new_task = {
        "task_id": f"TASK-{task_counter:03d}",
        "amr_id": task.amr_id,
        "task_type": task.task_type,
        "destination": destination,
        "pickup_station": pickup,
        "drop_station": drop,
        "priority": task.priority,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "done_at": None,
        "origin": str(amr.get("location") or ""),
        "progress_pct": 0.0,
        "path_total_m": 0.0,
        "auto_generated": False,
        "estimated_battery_cost_pct": round(est_cost, 2),
    }
    task_list.append(new_task)
    task_counter += 1
    return {"message": "작업 등록 완료", "task": new_task}


@app.patch("/tasks/{task_id}/start")
def start_task(task_id: str):
    task = next((t for t in task_list if t["task_id"] == task_id), None)
    if not task:
        return {"error": f"{task_id}를 찾을 수 없습니다"}

    amr = next((a for a in amr_list if a["id"] == task["amr_id"]), None)
    if not amr:
        return {"error": "AMR을 찾을 수 없습니다"}
    ensure_amr_shape(amr)

    err = start_task_for_amr(task, amr, amr_list, task_list)
    if err:
        return {"error": err}

    return {"message": f"{task_id} 시작", "task": task, "amr": amr}


@app.patch("/tasks/{task_id}/done")
def done_task(task_id: str):
    task = next((t for t in task_list if t["task_id"] == task_id), None)
    if not task:
        return {"error": f"{task_id}를 찾을 수 없습니다"}
    if task["status"] != "running":
        return {"error": "running 상태인 작업만 완료 처리 가능"}

    amr = next((a for a in amr_list if a["id"] == task["amr_id"]), None)
    if not amr:
        return {"error": "AMR을 찾을 수 없습니다"}
    ensure_amr_shape(amr)

    task["status"] = "done"
    task["done_at"] = datetime.now().isoformat()
    task["progress_pct"] = 100.0
    amr["active_task_id"] = None
    amr["status"] = "idle"
    clear_nav(amr)
    snap_amr_to_location(amr)
    record_task_completed(task)
    try_autostart_pending(amr, amr_list, task_list)

    return {"message": f"{task_id} 완료", "task": task, "amr": amr}


@app.delete("/tasks/{task_id}")
def cancel_task(task_id: str):
    for i, task in enumerate(task_list):
        if task["task_id"] == task_id:
            if task["status"] == "running":
                return {"error": "실행 중인 작업은 취소 불가"}
            task_list.pop(i)
            return {"message": f"{task_id} 취소 완료"}
    return {"error": f"{task_id}를 찾을 수 없습니다"}


# ─── 시나리오 Job 저장소 ──────────────────────────────────
# main.py의 시나리오 섹션 전체를 아래로 교체하세요.
# (기존 _run_single_scenario 함수와 /scenario/run 두 개 모두 삭제)

import uuid

# job_id → 상태 딕셔너리
# status: "running" | "done" | "error" | "cancelled"
_scenario_jobs: Dict[str, Dict[str, Any]] = {}


class ScenarioConfig(BaseModel):
    fleet_sizes: List[int] = Field(default=[5, 7, 9, 12])
    duration_s: float = Field(default=1800.0, ge=30, le=3600)
    job_interval_s: float = Field(default=4.0, ge=2, le=60)
    nav_speed: float = Field(default=2.0, ge=0.2, le=4.0)
    battery_drain_running: float = Field(default=0.18, ge=0.01, le=5.0)
    battery_charge_rate: float = Field(default=5.0, ge=0.5, le=20.0)
    sla_threshold_s: float = Field(default=300.0, ge=10, le=3600)


def _run_scenario_job(job_id: str, config: ScenarioConfig) -> None:
    """ThreadPoolExecutor에서 실행되는 동기 함수."""
    job = _scenario_jobs[job_id]
    fleet_sizes = config.fleet_sizes
    total = len(fleet_sizes)
    results: List[Dict[str, Any]] = []

    for idx, size in enumerate(fleet_sizes):
        if job.get("cancel_requested"):
            job["status"] = "cancelled"
            job["message"] = "사용자가 취소했습니다"
            return

        job["current_fleet_size"] = size
        job["current_index"] = idx
        job["message"] = f"{size}대 시뮬레이션 실행 중… ({idx + 1}/{total})"

        try:
            result = run_scenario_isolated(
                fleet_size=size,
                duration_s=config.duration_s,
                job_interval_s=config.job_interval_s,
                nav_speed=config.nav_speed,
                battery_drain=config.battery_drain_running,
                charge_rate=config.battery_charge_rate,
                sla_threshold_s=config.sla_threshold_s,
            )
            results.append(result)
            job["completed_sizes"].append(size)
            job["partial_results"] = list(results)
        except Exception as e:
            job["status"] = "error"
            job["message"] = f"{size}대 시뮬 오류: {e}"
            return

    # 추천 계산
    best_efficiency = max(results, key=lambda r: r["efficiency_score"])
    best_throughput = max(results, key=lambda r: r["throughput_per_hour"])
    best_sla = max(results, key=lambda r: r["sla_achievement_pct"])
    optimal = best_efficiency

    reasons: List[str] = [
        f"효율 점수 {optimal['efficiency_score']} (최고)",
        f"시간당 {optimal['throughput_per_hour']}건 처리",
        f"가동률 {optimal['utilization_run_pct']}%",
        f"SLA 달성률 {optimal['sla_achievement_pct']}%",
    ]
    if best_throughput["fleet_size"] != optimal["fleet_size"]:
        reasons.append(
            f"※ 처리량만 보면 {best_throughput['fleet_size']}대가 유리 "
            f"({best_throughput['throughput_per_hour']}건/h)"
        )
    if best_sla["fleet_size"] != optimal["fleet_size"]:
        reasons.append(
            f"※ SLA만 보면 {best_sla['fleet_size']}대가 유리 "
            f"({best_sla['sla_achievement_pct']}%)"
        )

    job["status"] = "done"
    job["message"] = "완료"
    job["result"] = {
        "config": config.model_dump(),
        "results": results,
        "recommendation": {
            "optimal_fleet_size": optimal["fleet_size"],
            "reason": " | ".join(reasons),
            "best_by_throughput": best_throughput["fleet_size"],
            "best_by_sla": best_sla["fleet_size"],
            "best_by_efficiency": best_efficiency["fleet_size"],
        },
    }


@app.post("/scenario/run")
async def run_scenario(config: ScenarioConfig):
    """시나리오 실행 시작 → job_id 즉시 반환."""
    job_id = str(uuid.uuid4())[:8]
    _scenario_jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "message": "시뮬레이션 준비 중…",
        "fleet_sizes": config.fleet_sizes,
        "total": len(config.fleet_sizes),
        "current_index": 0,
        "current_fleet_size": config.fleet_sizes[0] if config.fleet_sizes else 0,
        "completed_sizes": [],
        "partial_results": [],
        "cancel_requested": False,
        "result": None,
        "started_at": datetime.now().isoformat(),
    }
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_scenario_job, job_id, config)
    return {"job_id": job_id, "status": "running"}


@app.get("/scenario/status/{job_id}")
def get_scenario_status(job_id: str):
    """진행 상태 폴링."""
    job = _scenario_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id를 찾을 수 없습니다")

    total = job["total"]
    done_count = len(job["completed_sizes"])
    progress_pct = round(done_count / total * 100) if total > 0 else 0

    return {
        "job_id": job_id,
        "status": job["status"],
        "message": job["message"],
        "progress_pct": progress_pct,
        "current_fleet_size": job["current_fleet_size"],
        "current_index": job["current_index"],
        "total": total,
        "completed_sizes": job["completed_sizes"],
        "partial_results": job["partial_results"],
        "started_at": job["started_at"],
    }


@app.post("/scenario/cancel/{job_id}")
def cancel_scenario(job_id: str):
    """실행 중인 시나리오 취소 요청."""
    job = _scenario_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id를 찾을 수 없습니다")
    if job["status"] != "running":
        return {"message": f"이미 {job['status']} 상태입니다"}
    job["cancel_requested"] = True
    return {"message": "취소 요청됨"}


@app.get("/scenario/result/{job_id}")
def get_scenario_result(job_id: str):
    """완료된 시나리오 결과 조회."""
    job = _scenario_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id를 찾을 수 없습니다")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail=f"아직 완료되지 않았습니다 (status: {job['status']})")
    return job["result"]
    results = []
    for size in config.fleet_sizes:
        result = _run_single_scenario(
            fleet_size=size,
            duration_s=config.duration_s,
            job_interval_s=config.job_interval_s,
            nav_speed=config.nav_speed,
            battery_drain=config.battery_drain_running,
            charge_rate=config.battery_charge_rate,
        )
        results.append(result)

    best = max(results, key=lambda r: r["efficiency_score"])

    return {
        "config": config.model_dump(),
        "results": results,
        "recommendation": {
            "optimal_fleet_size": best["fleet_size"],
            "reason": (
                f"AMR {best['fleet_size']}대가 효율 점수 {best['efficiency_score']} 로 최고 "
                f"(시간당 {best['throughput_per_hour']}건 처리, "
                f"가동률 {best['utilization_run_pct']}%)"
            ),
        },
    }