"""Web server: serves the screens and bridges them to the engine.

  GET  /                  Moves screen (laptop / tablet)
  GET  /panel             touch panel for the Pi's screen
  GET  /setup             Motor Setup
  GET  /api/state         config, show, the catalogue of moves and their knobs
  PUT  /api/show          replace the show (live edits land here)
  PUT  /api/config        update installation settings (mechanics, motors, startup)
  GET  /ws                websocket: 20 Hz state out, commands in
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
from pathlib import Path

from aiohttp import WSMsgType, web

from . import motion as M
from . import protocol as P
from .config import KINDS, relayout_motors
from .engine import Engine

log = logging.getLogger("kinetic.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(engine: Engine) -> web.Application:
    app = web.Application()
    clients: set[web.WebSocketResponse] = set()

    async def index(_req):
        return web.FileResponse(WEB_DIR / "index.html")

    async def panel(_req):
        return web.FileResponse(WEB_DIR / "panel.html")

    async def commission(_req):
        return web.FileResponse(WEB_DIR / "commission.html")

    async def state(_req):
        return web.json_response({
            "config": engine.cfg.data,
            "show": engine.show,
            "moves": M.MOVES,
            "steps_per_mm": engine.cfg.steps_per_mm,
            "kinds": KINDS,
            "size_presets": engine.SIZE_PRESETS,
            "simulate": engine.cfg.data.get("simulate", False),
            "host": host_info(),
        })

    async def put_show(req):
        engine.save_show(await req.json())
        return web.json_response({"ok": True, "show": engine.show})

    async def put_config(req):
        body = await req.json()
        old_n = engine.cfg.motor_count
        if "layout_preset" in body:
            n = int(body.pop("layout_preset_count", engine.cfg.motor_count))
            body["motors"] = relayout_motors(engine.cfg.motors, max(1, min(16, n)), body.pop("layout_preset"))
        old_kinds = {m["id"]: m.get("kind", "spool") for m in engine.cfg.motors}
        engine.cfg.update(body)
        for m in engine.cfg.motors:
            before, after = old_kinds.get(m["id"], "spool"), m.get("kind", "spool")
            if before != after and "continuous" in (before, after):
                # switching between position and speed control: the taught setup no longer applies
                m["commissioned"] = False
                m.pop("bottom_fs", None)
                engine.event(f"{m.get('name', 'Motor')} changed to {after}: run Motor Setup for it", "warn")
        engine.cfg.save()
        engine.push_config()
        engine.push_limits()
        if engine.cfg.motor_count != old_n:
            engine.arm(False)
            engine.resize()
        return web.json_response({"ok": True, "config": engine.cfg.data, "steps_per_mm": engine.cfg.steps_per_mm})

    async def ws_handler(req):
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(req)
        clients.add(ws)
        await ws.send_json({"type": "events", "events": list(engine.events)})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                        log.debug("command %s", cmd.get("cmd"))
                        handle_command(engine, cmd)
                    except Exception as e:  # noqa: BLE001
                        log.warning("command failed: %s", e)
                        await ws.send_json({"type": "error", "msg": str(e)})
        finally:
            clients.discard(ws)
        return ws

    async def broadcaster(app_):
        async def loop():
            last_event_t = engine.events[-1]["t"] if engine.events else 0
            while True:
                await asyncio.sleep(0.05)
                if not clients:
                    continue
                snap = engine.snapshot()
                new_events = [e for e in engine.events if e["t"] > last_event_t]
                if new_events:
                    last_event_t = new_events[-1]["t"]
                    snap["events"] = new_events
                data = json.dumps(snap)
                for ws in list(clients):
                    try:
                        await ws.send_str(data)
                    except Exception:  # noqa: BLE001
                        clients.discard(ws)
        task = asyncio.create_task(loop())
        yield
        task.cancel()

    app.router.add_get("/", index)
    app.router.add_get("/panel", panel)
    app.router.add_get("/setup", commission)
    app.router.add_get("/api/state", state)
    app.router.add_put("/api/show", put_show)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", WEB_DIR)
    app.cleanup_ctx.append(broadcaster)
    return app


def host_info() -> dict:
    """Hostname and IP addresses, so the touch panel can show where the Moves screen lives."""
    ips = []
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=1).stdout
        ips = [ip for ip in out.split() if ":" not in ip]
    except Exception:  # noqa: BLE001
        pass
    return {"hostname": socket.gethostname(), "ips": ips}


def _target(msg) -> int:
    t = msg.get("id", "all")
    return P.TARGET_ALL if t in (None, "all") else int(t)


def handle_command(engine: Engine, msg: dict):
    c = msg.get("cmd")
    if c == "play":
        engine.play()
    elif c == "set_move":
        engine.set_move(**{k: float(v) for k, v in (msg.get("knobs") or {}).items()})
    elif c == "hold":
        engine.hold()
    elif c == "park":
        engine.park()
    elif c == "arm":
        engine.arm(bool(msg.get("on", True)))
    elif c == "stop_all":
        engine.stop_nodes()
    elif c == "home":
        engine.home(_target(msg))
    elif c == "set_zero":
        engine.set_zero(_target(msg))
    elif c == "jog":
        engine.jog(_target(msg), float(msg["mm"]))
    elif c == "identify":
        engine.identify(msg["mac"])
    elif c == "assign":
        engine.assign(msg["mac"], int(msg["new_id"]))
    elif c == "enable":
        engine.enable(_target(msg), bool(msg.get("on", True)))
    elif c == "push_config":
        engine.push_config(_target(msg))
    elif c == "autostart":
        asyncio.get_running_loop().create_task(engine.autostart())
    # ---- adding / removing motors
    elif c == "add_motor":
        engine.add_motor()
    elif c == "remove_motor":
        engine.remove_motors([int(msg["id"])])
    elif c == "set_motor_count":
        engine.set_motor_count(int(msg["n"]), bool(msg.get("force")))
    elif c == "remove_unused":
        engine.remove_unused()
    # ---- Motor Setup wizard
    elif c == "wiz_start":
        engine.wiz_start(msg["mac"], int(msg["motor"]), bool(msg.get("recover")), bool(msg.get("speed_only")))
    elif c == "wiz_speed":
        engine.wiz_speed_test()
    elif c == "wiz_speed_stop":
        engine.wiz_speed_stop()
    elif c == "wiz_speed_save":
        engine.wiz_speed_save(bool(msg.get("use", True)))
    elif c == "wiz_jog":
        engine.wiz_jog(float(msg["mm"]))
    elif c == "wiz_stop":
        engine.wiz_speed_stop()
        if engine.wizard:
            engine.send_cmd(P.C_STOP, engine.wizard["motor"])
    elif c == "wiz_dir_test":
        engine.wiz_direction_test()
    elif c == "wiz_dir_answer":
        engine.wiz_direction_answer(bool(msg.get("correct", msg.get("lowered"))))
    elif c == "wiz_mech":
        engine.wiz_mechanism(str(msg["kind"]), msg.get("size"), msg.get("bidirectional"))
    elif c == "wiz_encoder":
        engine.wiz_encoder_check()
    elif c == "wiz_top":
        engine.wiz_set_top()
    elif c == "wiz_bottom":
        engine.wiz_set_bottom()
    elif c == "wiz_test":
        engine.wiz_test()
    elif c == "wiz_goto":
        engine.wiz_goto(str(msg["step"]))
    elif c == "wiz_finish":
        engine.wiz_finish()
    elif c == "wiz_cancel":
        engine.wiz_cancel()
    # ---- simulator only
    elif c == "sim_power_cut" and hasattr(engine.tx, "power_cut"):
        engine.last_known.clear()        # a real power cut takes the Pi down too
        engine.arm(False)
        engine.tx.power_cut(save_works=bool(msg.get("save_works", True)))
        engine.event("SIMULATOR: power cut to every motor" +
                     ("" if msg.get("save_works", True) else " (power-loss save failed)"), "warn")
    elif c == "sim_reboot" and hasattr(engine.tx, "reboot_node"):
        engine.tx.reboot_node(msg["mac"])
        engine.event(f"SIMULATOR: node {msg['mac']} rebooted", "warn")
    else:
        raise ValueError(f"unknown command {c}")
