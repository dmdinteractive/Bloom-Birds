#!/usr/bin/env python3
"""Start the kinetic sculpture controller.

    python3 run.py                 # real hardware (bridge on USB)
    python3 run.py --simulate      # no hardware: simulated motors
    python3 run.py --port 8080     # web editor port (default 8080)

Testing on a Mac (see mac/README-mac.md, or just double-click mac/Start Simulator.command):

    python3 run.py --simulate --axes 5 --local --open --config config-sim.json

Then open http://<pi-address>:8080 in a browser.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import webbrowser
from pathlib import Path

from aiohttp import web

from kinetic.config import Config, relayout_motors
from kinetic.engine import Engine
from kinetic.server import create_app
from kinetic.triggers import TriggerManager
from kinetic.transport import SerialTransport, SimTransport

HERE = Path(__file__).resolve().parent


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--shows", default=str(HERE / "shows"))
    ap.add_argument("--simulate", action="store_true", help="run without hardware")
    ap.add_argument("--axes", type=int, default=None,
                    help="number of motors (1-16); changes the motor list in the config")
    ap.add_argument("--serial", default=None, help="bridge serial port (default: auto)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--local", action="store_true",
                    help="only reachable from this computer (no firewall prompt on a Mac)")
    ap.add_argument("--open", action="store_true", help="open the editor and panel in the browser")
    ap.add_argument("--no-autostart", action="store_true", help="don't home/play at startup")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config(args.config)
    if args.axes is not None:
        n = max(1, min(16, args.axes))
        if cfg.motor_count != n:
            cfg.data["motors"] = relayout_motors(cfg.motors, n, cfg.data.get("layout", "line"))
    simulate = args.simulate or cfg.data.get("simulate", False)
    cfg.data["simulate"] = simulate
    cfg.save()

    if simulate:
        # The simulated motors keep their "flash" (taught top, limits, saved position) and their
        # physical positions in a file next to the config, so restarting the simulator acts
        # like a power cycle.
        state = Path(args.config).with_name(Path(args.config).stem + "-nodes.json")
        travel_steps = int(cfg.mech["travel_mm"] * cfg.steps_per_mm)
        transport = SimTransport(cfg.motor_count, cfg.node_config_args(), unassigned=1,
                                 state_path=state, travel_steps=travel_steps)
        if transport.fresh:
            # the simulated motors come already set up (except the extra, unassigned board)
            full = cfg.mech["travel_mm"] / (3.141592653589793 * cfg.mech["spool_diameter_mm"]) * cfg.mech["full_steps_per_rev"]
            for i, m in enumerate(cfg.motors):
                m.update({"commissioned": True, "bottom_fs": round(full, 2), "travel_mm": None,
                          "node_mac": f"5E:00:00:00:00:{i:02X}"})
                m["invert_dir"] = False
                m["invert_encoder"] = False
            cfg.mech["home_mode"] = "memory"
            cfg.save()
    else:
        transport = SerialTransport(args.serial or cfg.data.get("serial_port", "auto"))

    engine = Engine(cfg, transport, Path(args.shows))
    transport.start()

    app = create_app(engine)
    runner = web.AppRunner(app)
    await runner.setup()
    host = "127.0.0.1" if args.local else "0.0.0.0"
    try:
        await web.TCPSite(runner, host, args.port).start()
    except OSError as e:
        raise SystemExit(f"\nPort {args.port} is already in use ({e.strerror}). Is the controller "
                         f"already running in another window? Close it, or start with --port {args.port + 1}.\n")
    url = f"http://localhost:{args.port}"
    logging.info("Editor running at %s  (%s, %d axes)", url,
                 "SIMULATION" if simulate else "hardware", cfg.motor_count)
    logging.info("Touch panel at   %s/panel", url)
    if args.open:
        webbrowser.open(url)
        webbrowser.open(url + "/panel")

    # Stop cleanly on Ctrl+C, on closing the Terminal window (SIGHUP) and when systemd stops the
    # service (SIGTERM): the simulator saves its motors' memory, the serial port is released.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
    # external triggers (OSC, MIDI, Art-Net, GPIO): only the inputs switched on in the editor start
    engine.triggers = TriggerManager(engine)
    await engine.triggers.start()
    runner_task = asyncio.create_task(engine.run())
    if not args.no_autostart:
        asyncio.create_task(engine.autostart())
    try:
        await asyncio.wait([runner_task, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
    finally:
        logging.info("Shutting down")
        engine.triggers.stop()
        transport.stop()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
