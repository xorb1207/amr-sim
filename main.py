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
    refresh_station_cache,
    rescue_warp_amr,
    set_nav_path_for_destination,
    sim_params,
    snap_amr_to_location,
    start_task_for_amr,
    station_claimed_by_other,
    tick,
    try_autostart_pending,
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
        f"Simulation time (s): {s['sim_time_s']}",
        f"Fleet size: {s['fleet_size']}",
        f"Tasks completed: {s['tasks_completed']}",
        f"Tasks started: {s['tasks_started']}",
        f"Total transport (m): {s['total_transport_m']}",
        f"Fleet run utilization (%): {s['utilization_run_pct']}",
        f"Fleet idle (%): {s['utilization_idle_pct']}",
        f"Avg pending wait (s/robot): {s['avg_pending_wait_s']}",
        "",
        "Operational params:",
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
    record_task_completed()
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



# ─── 시나리오 시뮬레이션 API ─────────────────────────

class ScenarioConfig(BaseModel):
    fleet_sizes: List[int] = Field(default=[3, 5, 7])
    duration_s: float = Field(default=300.0, ge=30, le=3600)
    job_interval_s: float = Field(default=7.0, ge=2, le=60)
    nav_speed: float = Field(default=1.35, ge=0.2, le=4.0)
    battery_drain_running: float = Field(default=0.8, ge=0.1, le=5.0)
    battery_charge_rate: float = Field(default=3.0, ge=0.5, le=20.0)


def _run_single_scenario(
    fleet_size: int,
    duration_s: float,
    job_interval_s: float,
    nav_speed: float,
    battery_drain: float,
    charge_rate: float,
) -> Dict[str, Any]:
    from wave_map import get_config as _gc
    from simulation import (
        make_amr, refresh_station_cache,
        STATIONS, CHARGING_STATIONS,
        ensure_amr_shape, compute_acs_state,
        station_claimed_by_other,
        evaluate_transport_energy_budget,
        start_task_for_amr,
        clear_nav, snap_amr_to_location,
        nearest_free_charger,
        ARRIVAL_EPS, CONFLICT_DIST_M,
        CHARGE_TRIGGER_PCT, CHARGE_COMPLETE_PCT,
        LOW_BATTERY_FORCE_CHARGE_PCT,
        station_coords,
    )
    import math, random
    from datetime import datetime as _dt
    from collections import deque

    refresh_station_cache()

    local_params = {
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

    local_analytics = {
        "sim_time_s": 0.0,
        "tasks_completed": 0,
        "tasks_started": 0,
        "total_transport_m": 0.0,
        "robot_running_s": {},
        "robot_idle_s": {},
        "robot_wait_pending_s": {},
    }

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
    task_counter = [1]
    charger_res: Dict[str, str] = {}
    internal_ctr = [1]

    def _try_autostart(a: Dict[str, Any]) -> None:
        if a["status"] != "idle":
            return
        pending = [t for t in tasks if t.get("amr_id") == a["id"] and t.get("status") == "pending"]
        if not pending:
            return
        pending.sort(key=lambda t: (-int(t.get("priority", 1)), str(t.get("created_at", ""))))
        for t in pending:
            if start_task_for_amr(t, a, amrs, tasks) is None:
                local_analytics["tasks_started"] += 1
                return

    def _begin_charge_local(a: Dict[str, Any]) -> None:
        ch = nearest_free_charger(a["id"], float(a["x"]), float(a["y"]), amrs, tasks)
        if not ch:
            return
        charger_res[ch] = a["id"]
        tid = f"CHG-{internal_ctr[0]:04d}"
        internal_ctr[0] += 1
        ct = {
            "task_id": tid, "amr_id": a["id"],
            "task_type": "charge", "destination": ch,
            "priority": 99, "status": "pending",
            "created_at": _dt.now().isoformat(),
            "started_at": None, "done_at": None,
            "origin": str(a.get("location", "")),
            "progress_pct": 0.0, "path_total_m": 0.0,
            "auto_generated": True,
        }
        tasks.append(ct)
        if start_task_for_amr(ct, a, amrs, tasks) is None:
            local_analytics["tasks_started"] += 1

    def _spawn_job() -> None:
        dests = [s for s in STATIONS if s not in CHARGING_STATIONS]
        if not dests:
            return
        destination = random.choice(dests)
        candidates = []
        for a in amrs:
            ensure_amr_shape(a)
            if a["status"] != "idle" or compute_acs_state(a) != "Idle":
                continue
            if float(a["battery"]) <= LOW_BATTERY_FORCE_CHARGE_PCT:
                continue
            if any(t.get("amr_id") == a["id"] and t.get("status") in ("pending", "running") for t in tasks):
                continue
            if station_claimed_by_other(destination, amrs, tasks, a["id"]):
                continue
            ok, _, cost, _ = evaluate_transport_energy_budget(
                a, str(a.get("location", "")), destination, amrs, tasks
            )
            if ok:
                candidates.append((cost, a))
        if not candidates:
            return
        candidates.sort(key=lambda x: x[0])
        _, amr = candidates[0]
        tid = f"SC-{fleet_size}-{task_counter[0]:04d}"
        task_counter[0] += 1
        t = {
            "task_id": tid, "amr_id": amr["id"],
            "task_type": "move", "destination": destination,
            "pickup_station": str(amr.get("location", "")),
            "drop_station": destination,
            "priority": random.randint(1, 5),
            "status": "pending",
            "created_at": _dt.now().isoformat(),
            "started_at": None, "done_at": None,
            "origin": str(amr.get("location", "")),
            "progress_pct": 0.0, "path_total_m": 0.0,
            "auto_generated": True,
        }
        tasks.append(t)
        if start_task_for_amr(t, amr, amrs, tasks) is None:
            local_analytics["tasks_started"] += 1

    def local_tick(dt: float) -> None:
        nav = local_params["nav_speed"]
        dr_run = local_params["battery_drain_running"]
        dr_idle = local_params["battery_drain_idle"]
        dr_nop = local_params["battery_drain_running_nopower"]
        chg = local_params["battery_charge_rate"]
        local_analytics["sim_time_s"] += dt

        running = [a for a in amrs if a["status"] == "running" and a.get("path_queue")]
        yield_ids = set()
        by_wp: Dict[tuple, list] = {}
        for a in running:
            w = a["path_queue"][0]
            key = (round(w[0]*50), round(w[1]*50))
            by_wp.setdefault(key, []).append(a["id"])
        for aids in by_wp.values():
            if len(aids) >= 2:
                aids.sort()
                yield_ids.update(aids[1:])
        for i in range(len(running)):
            for j in range(i+1, len(running)):
                a1, a2 = running[i], running[j]
                d = math.hypot(float(a1["x"])-float(a2["x"]), float(a1["y"])-float(a2["y"]))
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

            local_analytics["robot_running_s"].setdefault(aid, 0.0)
            local_analytics["robot_idle_s"].setdefault(aid, 0.0)
            local_analytics["robot_wait_pending_s"].setdefault(aid, 0.0)

            b = float(a["battery"])
            has_pending = any(t.get("amr_id") == aid and t.get("status") == "pending" for t in tasks)
            if st == "idle" and has_pending:
                local_analytics["robot_wait_pending_s"][aid] += dt
            elif st == "idle":
                local_analytics["robot_idle_s"][aid] += dt
            elif st == "running":
                local_analytics["robot_running_s"][aid] += dt

            if b <= 0.0:
                a["battery"] = 0.0
                a["status"] = "error"
                clear_nav(a)
                a["active_task_id"] = None
                continue

            if st == "running":
                if b < CHARGE_TRIGGER_PCT and not a.get("low_bat_flag"):
                    a["low_bat_flag"] = True
                    a["charge_after_job"] = True
                target = None
                pq = a.get("path_queue") or []
                if pq:
                    target = (float(pq[0][0]), float(pq[0][1]))
                if target and not a.get("acs_yield"):
                    tx, ty = target
                    x, y = float(a["x"]), float(a["y"])
                    dx, dy = tx-x, ty-y
                    dist = math.hypot(dx, dy)
                    if dist < ARRIVAL_EPS:
                        a["x"], a["y"] = tx, ty
                        a["vx"] = a["vy"] = 0.0
                        pq.pop(0)
                        a["path_queue"] = pq
                        local_analytics["total_transport_m"] += dist
                    else:
                        step = min(nav*dt, dist)
                        ux, uy = dx/dist, dy/dist
                        a["x"] += ux*step
                        a["y"] += uy*step
                        a["vx"], a["vy"] = ux*nav, uy*nav
                        a["yaw"] = math.atan2(dy, dx)
                        local_analytics["total_transport_m"] += step
                    b = max(0.0, b - dr_run*dt)
                else:
                    a["vx"] = a["vy"] = 0.0
                    b = max(0.0, b - dr_nop*dt)
            elif st == "charging":
                a["vx"] = a["vy"] = 0.0
                b = min(100.0, b + chg*dt)
                if b >= CHARGE_COMPLETE_PCT:
                    a["status"] = "idle"
                    a["low_bat_flag"] = False
                    _try_autostart(a)
            else:
                a["vx"] = a["vy"] = 0.0
                b = max(0.0, b - dr_idle*dt)

            a["battery"] = round(max(0.0, min(100.0, b)), 2)

        for a in amrs:
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
            if math.hypot(float(a["x"])-tx, float(a["y"])-ty) > ARRIVAL_EPS*3:
                continue
            task["status"] = "done"
            task["done_at"] = _dt.now().isoformat()
            task["progress_pct"] = 100.0
            local_analytics["tasks_completed"] += 1
            a["active_task_id"] = None
            a["location"] = dest
            clear_nav(a)
            snap_amr_to_location(a)
            if dest in CHARGING_STATIONS:
                if charger_res.get(dest) == a["id"]:
                    del charger_res[dest]
                a["status"] = "charging"
                a["low_bat_flag"] = False
            else:
                a["status"] = "idle"
                if float(a["battery"]) < CHARGE_TRIGGER_PCT or a.get("charge_after_job"):
                    a["charge_after_job"] = False
                    _begin_charge_local(a)
                else:
                    _try_autostart(a)

    hz = 50.0
    dt = 1.0 / hz
    steps = int(duration_s * hz)
    dispatch_accum = 0.0

    for _ in range(steps):
        local_tick(dt)
        dispatch_accum += dt
        if dispatch_accum >= job_interval_s:
            dispatch_accum = 0.0
            _spawn_job()

    n = max(fleet_size, 1)
    t_total = max(local_analytics["sim_time_s"], 1e-6)
    capacity = t_total * n
    run_sum = sum(local_analytics["robot_running_s"].get(a["id"], 0.0) for a in amrs)
    idle_sum = sum(local_analytics["robot_idle_s"].get(a["id"], 0.0) for a in amrs)
    completed = local_analytics["tasks_completed"]
    error_count = sum(1 for a in amrs if a.get("status") == "error")
    avg_battery = sum(float(a.get("battery", 0)) for a in amrs) / n

    return {
        "fleet_size": fleet_size,
        "duration_s": duration_s,
        "tasks_completed": completed,
        "tasks_started": local_analytics["tasks_started"],
        "throughput_per_hour": round(completed / (duration_s / 3600), 1),
        "utilization_run_pct": round(100.0 * run_sum / capacity, 2),
        "utilization_idle_pct": round(100.0 * idle_sum / capacity, 2),
        "avg_pending_wait_s": round(
            sum(local_analytics["robot_wait_pending_s"].get(a["id"], 0.0) for a in amrs) / n, 2
        ),
        "total_transport_m": round(local_analytics["total_transport_m"], 2),
        "error_amr_count": error_count,
        "avg_battery_end_pct": round(avg_battery, 1),
        "tasks_per_amr": round(completed / n, 1),
        "efficiency_score": round(
            (completed / n)
            * (1 - error_count / n)
            * (run_sum / capacity if capacity > 0 else 0),
            3
        ),
    }


@app.post("/scenario/run")
def run_scenario(config: ScenarioConfig):
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