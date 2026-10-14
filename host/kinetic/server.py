"""Web server: serves the editor and bridges it to the engine.

  GET  /                  editor page
  GET  /api/state         config, show, pattern catalogue, list of shows
  PUT  /api/show          replace the active show (live edits land here)
  POST /api/show/saveas   {"name": ...}
  POST /api/show/load     {"name": ...}
  PUT  /api/config        update installation settings (mechanics, motors, startup)
  POST /api/render        {"clip": {...}} -> sampled motion for the editor graphs
  GET  /api/triggers      external trigger settings, input status, MIDI devices
  PUT  /api/triggers      save trigger settings and restart the inputs
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
from . import triggers as T

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
            "shows": engine.list_shows(),
            "patterns": M.PATTERNS,
            "mod_params": M.MOD_PARAMS,
            "blends": M.BLENDS,
            "common_params": M.COMMON_PARAMS,
            "eases": M.EASES,
            "steps_per_mm": engine.cfg.steps_per_mm,
            "kinds": KINDS,
            "size_presets": engine.SIZE_PRESETS,
            "simulate": engine.cfg.data.get("simulate", False),
            "host": host_info(),
        })

    async def put_show(req):
        show = await req.json()
        if show.get("name") and show.get("name") != engine.show.get("name"):
            # the active show was switched (by a trigger or another screen) after this editor
            # loaded it: don't let a late save overwrite the new one
            return web.json_response({"error": f"The active show is now \"{engine.show.get('name')}\""}, status=409)
        engine.save_show(show)
        return web.json_response({"ok": True})

    async def save_as(req):
        body = await req.json()
        engine.save_show(engine.show, body["name"])
        engine.cfg.data["active_show"] = engine.show["name"]
        engine.cfg.save()
        return web.json_response({"ok": True, "shows": engine.list_shows(), "show": engine.show})

    async def load(req):
        body = await req.json()
        engine.hold()
        engine.load_show(body["name"])
        engine.show_rev += 1
        engine.cfg.save()
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

    async def render(req):
        body = await req.json()
        cfg = engine.cfg
        travel = [cfg.travel(i) for i in range(cfg.motor_count)]
        n = cfg.motor_count
        # continuous motors: the curve is a speed, so the limit that matters is how fast the
        # speed may change (acceleration)
        vmax = [cfg.amax(i) if cfg.is_continuous(i) else cfg.vmax(i) for i in range(n)]
        amax = [cfg.amax(i) * 20 if cfg.is_continuous(i) else cfg.amax(i) for i in range(n)]
        try:
            ctx = engine.eval_ctx()
            if isinstance(body.get("clips"), list):     # the editor's unsaved clip library
                ctx["clips"] = {c["id"]: c for c in body["clips"] if isinstance(c, dict) and "id" in c}
            clip = body["clip"]
            kind, base = M.time_base(clip)
            # sample fast motion finely enough to draw it (a 0.5 s flap needs more than 10/s)
            sps = min(100.0, max(10.0, 30.0 / base)) if kind == "period" else 10.0
            cont = [cfg.is_continuous(i) for i in range(n)]

            def units(i, v):
                return cfg.speed_from_norm(i, v) if cont[i] else v * travel[i]

            def work():
                d = M.render_clip(clip, cfg.motors, travel, vmax, amax, ctx=ctx, samples_per_s=sps,
                                  max_samples=2400)
                d["fit"] = M.clip_fit(clip, cfg.motors, units, [cfg.vmax(i) for i in range(n)],
                                      [cfg.amax(i) for i in range(n)], cont, ctx)
                return d
            data = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data)

    def triggers_info() -> dict:
        tm = engine.triggers
        return {"config": tm.cfg if tm else T.DEFAULTS, "actions": T.ACTIONS, "sources": T.SOURCES,
                "status": tm.status if tm else {}, "midi_devices": T.midi_devices(),
                "shows": engine.list_shows(), "available": tm is not None}

    async def get_triggers(_req):
        return web.json_response(triggers_info())

    async def put_triggers(req):
        if not engine.triggers:
            return web.json_response({"error": "triggers are not running"}, status=503)
        engine.triggers.update(await req.json())
        await engine.triggers.apply()
        return web.json_response(triggers_info())

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
    app.router.add_post("/api/show/saveas", save_as)
    app.router.add_post("/api/show/load", load)
    app.router.add_put("/api/config", put_config)
    app.router.add_post("/api/render", render)
    app.router.add_get("/api/triggers", get_triggers)
    app.router.add_put("/api/triggers", put_triggers)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", WEB_DIR)
    app.cleanup_ctx.append(broadcaster)
    return app


def host_info() -> dict:
    """Hostname and IP addresses, so the touch panel can show where the full editor lives."""
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
    elif c == "play_entry":
        engine.play_entry(int(msg["index"]))
    elif c == "trigger_learn" and engine.triggers:
        engine.triggers.learn = msg.get("id") or None
    elif c == "trigger_test" and engine.triggers:
        engine.triggers.test(str(msg["id"]))
    elif c == "trigger_inject" and engine.triggers:      # pretend an input arrived (testing)
        engine.triggers.handle(str(msg["source"]), str(msg["address"]), float(msg.get("value", 1.0)))
    elif c == "set_playback":
        engine.set_playback(msg.get("speed"), msg.get("depth"))
    elif c == "next":
        engine.next()
    elif c == "solo":
        engine.solo(msg["clip"])
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
