"""
PZEM → OPC UA writer (quiet by default)

WHAT THIS IS
------------
A minimal bridge that reads four electrical metrics from a PZEM-like energy
sensor over UART and writes them into an OPC UA server at the locations
expected by UmatiPlasticsRubberGenericType:

  • AcVoltagePe
  • AcCurrentPe
  • AcActivePowerPe
  • AcActiveEnergyTotalImportHp

The client is intentionally quiet: it logs only lifecycle events (start,
connect, disconnect) and warnings/errors. Detailed value logs can be enabled
via config.

HOW WE BUILD IT (STEP-BY-STEP)
------------------------------
1) Load configuration and set up logging (console-only, quiet third-party libs).
2) Create a PzemNative reader:
   - Either read raw frames from serial (single request → 25-byte response)
   - Or synthesize data in "simulate" mode for local testing.
3) Connect an asyncua Client to the OPC UA endpoint.
4) Resolve the target variable nodes by browsing:
   <machine_root> / Monitoring / Consumption / Electricity / Main
   and pick the 4 variables by browseName.
5) Enter a loop:
   - Read sensor data, scale with config factors
   - Write doubles to the four OPC UA nodes
   - Sleep for configured sample interval
6) On Ctrl+C / cancellation, disconnect gracefully.

CONFIG KEYS (ESSENTIAL)
-----------------------
application:
  log_level: INFO|WARNING|DEBUG (default INFO)
  verbose: bool (enables extra DEBUG from our code only)
  quiet_third_party: bool (mute asyncua/websockets/etc. to WARNING)
  log_values: bool (periodic value logs)
  log_values_every_n: int (log every N cycles when log_values=true)

opcua:
  server_url: opc.tcp://...
  machine_root_nodeid: "ns=...;i=..." of the machine root
  channel_path: ["Monitoring", "Consumption", "Electricity", "Main"]
  variables: ["AcVoltagePe", "AcCurrentPe", "AcActivePowerPe", "AcActiveEnergyTotalImportHp"]
  retries, retry_delay_sec: connection retry policy

pzem:
  device: /dev/ttyAMA0
  baudrate: 9600
  timeout_sec: 2.0
  read_delay_sec: 0.2
  simulate: false

scales:
  voltage_scale, current_scale, power_scale, energy_scale

timing:
  sample_interval_sec: 5.0

EXTENDING
---------
• Add more variables: include browseNames in config.opcua.variables and write them.
• Change sensor: swap PzemNative with another reader, keep the same reading fields.
• Add metrics/prometheus or file logging: see recommendations below.
"""

# ─────────────────────────────────────────────────────────────────
# 1) Configuration & Logging
# ─────────────────────────────────────────────────────────────────

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import serial
from asyncua import Client, ua


def load_config(path: str = "config.json") -> Dict:
    """Load JSON config or raise if missing."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def setup_logging(cfg: Dict) -> None:
    """
    Configure console logging:
    - Our root logger uses 'log_level' (INFO by default)
    - Third-party libraries are muted to WARNING when 'quiet_third_party' is true
    """
    app = cfg.get("application", {})
    lvl_name = str(app.get("log_level", "INFO")).upper()
    verbose = bool(app.get("verbose", False))
    quiet_third = bool(app.get("quiet_third_party", True))

    level = getattr(logging, lvl_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s: %(message)s")

    # Root logger adopts chosen level; enable DEBUG for our code when verbose
    logging.getLogger().setLevel(logging.DEBUG if verbose else level)

    # Silence noisy third-party loggers unless explicitly disabled
    third_party_loggers = [
        "asyncua",
        "asyncua.client",
        "asyncua.common",
        "asyncua.crypto",
        "asyncua.sync",
        "asyncua.ua",
        "asyncua.transport",
        "websockets",
        "urllib3",
        "serial",
    ]
    for name in third_party_loggers:
        logging.getLogger(name).setLevel(logging.WARNING if quiet_third else level)


# ─────────────────────────────────────────────────────────────────
# 2) Sensor: PZEM reader (native frame)
# ─────────────────────────────────────────────────────────────────

@dataclass
class PzemReading:
    """Single snapshot of electrical readings (engineering units)."""
    voltage: float = 0.0   # V
    current: float = 0.0   # A
    power: float = 0.0     # W
    energy: float = 0.0    # Wh
    status: str = "OK"     # "OK" | "SIM" | error string


class PzemNative:
    """
    Minimal PZEM-017/004T-like reader using a single request that returns a
    25-byte response. We either decode real serial data or synthesize values
    in simulate mode for development.

    Notes:
    • This is intentionally small and dependency-light (no pymodbus).
    • Energy accumulates in Wh between reads in simulate mode.
    """

    def __init__(self, device: str, baudrate: int, timeout: float,
                 read_delay: float, simulate: bool = False) -> None:
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self.read_delay = read_delay
        self.simulate = simulate
        self._last_energy = 0.0

    @staticmethod
    def _decode_response(buf: bytes) -> Optional[PzemReading]:
        """Decode a 25-byte PZEM response buffer into engineering values."""
        if len(buf) < 25:
            return None
        try:
            voltage = struct.unpack(">H", buf[3:5])[0] / 10.0            # 0.1 V
            current = struct.unpack(">I", b"\x00" + buf[5:8])[0] / 1000.0  # mA → A
            power   = struct.unpack(">I", b"\x00" + buf[9:12])[0] / 10.0   # 0.1 W
            energy  = float(struct.unpack(">I", b"\x00" + buf[13:16])[0])  # Wh
            return PzemReading(voltage, current, power, energy, "OK")
        except Exception:
            return None

    def read(self) -> PzemReading:
        """
        Return current sensor reading:
        - Simulate: synthetic but realistic values
        - Real: send a request, wait briefly, parse a 25-byte response
        """
        if self.simulate:
            now = time.time()
            voltage = 229.0 + 0.5 * (1 if int(now) % 2 == 0 else -1)
            current = 0.150 + 0.010 * (1 if int(now / 3) % 2 == 0 else -1)
            power = voltage * current
            # Accumulate energy with sample interval assumption (~5s typical)
            self._last_energy += power * (5.0 / 3600.0)  # Wh
            return PzemReading(voltage, current, power, self._last_energy, "SIM")

        try:
            with serial.Serial(self.device, self.baudrate, timeout=self.timeout) as ser:
                # One command → one response (fixed frame)
                req = bytes([0x01, 0x04, 0x00, 0x00, 0x00, 0x0A, 0x70, 0x0D])
                ser.write(req)
                time.sleep(self.read_delay)
                resp = ser.read(25)
                reading = self._decode_response(resp)
                if reading:
                    self._last_energy = reading.energy
                    return reading
                return PzemReading(status="No response/Decode fail")
        except Exception as e:
            return PzemReading(status=f"Serial error: {e}")


# ─────────────────────────────────────────────────────────────────
# 3) OPC UA writer (Umati PR Generic channel)
# ─────────────────────────────────────────────────────────────────

class UmatiPrWriter:
    """
    Resolves and writes the four expected variables under:
      <machine_root> / Monitoring / Consumption / Electricity / Main

    We locate nodes by browseName to keep the client configuration stable even
    if NodeIds differ between environments.
    """

    def __init__(
            self,
            client: Client,
            machine_root_id: ua.NodeId,
            channel_path: list[str],
            variable_names: list[str],
            verbose: bool = False,
    ) -> None:
        self.client = client
        self.machine_root_id = machine_root_id
        self.channel_path = channel_path
        self.variable_names = variable_names
        self.verbose = verbose
        self.nodes: Dict[str, "asyncua.Node"] = {}

    async def _find_child_by_browse_name(self, parent, browse_name: str):
        """Return the child node whose browseName matches exactly, else None."""
        refs = await parent.get_children()
        for n in refs:
            qn = await n.read_browse_name()
            if qn.Name == browse_name:
                return n
        return None

    async def resolve_nodes(self) -> bool:
        """Walk the channel_path and pick variable nodes by browseName."""
        root = self.client.get_node(self.machine_root_id)
        cur = root
        for segment in self.channel_path:
            nxt = await self._find_child_by_browse_name(cur, segment)
            if nxt is None:
                logging.error("Browse failed at segment '%s'", segment)
                return False
            cur = nxt

        for name in self.variable_names:
            node = await self._find_child_by_browse_name(cur, name)
            if node is None:
                logging.error("Variable '%s' not found under channel", name)
                return False
            self.nodes[name] = node

        if self.verbose:
            # Optional: inspect access levels at DEBUG level
            for name, node in self.nodes.items():
                attrs = await node.read_attributes(
                    [ua.AttributeIds.AccessLevel, ua.AttributeIds.UserAccessLevel]
                )
                al = int(attrs[0].Value.Value)
                ual = int(attrs[1].Value.Value)
                logging.debug(
                    "Node %-34s AccessLevel=0x%02X UserAccessLevel=0x%02X", name, al, ual
                )
        return True

    async def write_values(self, values: Dict[str, float]) -> bool:
        """Write each variable individually as Double; return True if all queued writes succeeded."""
        try:
            ok = True
            for name in self.variable_names:
                node = self.nodes.get(name)
                if not node:
                    logging.error("Node not resolved: %s", name)
                    ok = False
                    continue
                v = float(values.get(name, 0.0))
                await node.write_value(ua.Variant(v, ua.VariantType.Double))
            return ok
        except Exception as e:
            logging.error("UA write exception: %s", e)
            return False


# ─────────────────────────────────────────────────────────────────
# 4) Orchestration & Main loop
# ─────────────────────────────────────────────────────────────────

async def main() -> None:
    """Top-level orchestration: config → sensor → connect → resolve → loop → disconnect."""
    cfg = load_config()
    setup_logging(cfg)
    log = logging.getLogger("main")

    app_cfg = cfg.get("application", {})
    verbose = bool(app_cfg.get("verbose", False))
    log_values = bool(app_cfg.get("log_values", False))
    values_every_n = int(app_cfg.get("log_values_every_n", 12))  # log every N cycles

    # Sensor setup
    pzem_cfg = cfg.get("pzem", {})
    pzem = PzemNative(
        device=pzem_cfg.get("device", "/dev/ttyAMA0"),
        baudrate=int(pzem_cfg.get("baudrate", 9600)),
        timeout=float(pzem_cfg.get("timeout_sec", 2.0)),
        read_delay=float(pzem_cfg.get("read_delay_sec", 0.2)),
        simulate=bool(pzem_cfg.get("simulate", False)),
    )

    # Scaling factors
    sc = cfg.get("scales", {})
    v_scale = float(sc.get("voltage_scale", 1.0))
    i_scale = float(sc.get("current_scale", 1.0))
    p_scale = float(sc.get("power_scale", 1.0))
    e_scale = float(sc.get("energy_scale", 1.0))

    # OPC UA settings
    ua_cfg = cfg.get("opcua", {})
    endpoint = ua_cfg.get("server_url", "opc.tcp://127.0.0.1:4840")
    machine_root = ua_cfg.get("machine_root_nodeid", "ns=1;i=74000")
    channel_path = ua_cfg.get("channel_path", ["Monitoring", "Consumption", "Electricity", "Main"])
    var_names = ua_cfg.get(
        "variables",
        ["AcVoltagePe", "AcCurrentPe", "AcActivePowerPe", "AcActiveEnergyTotalImportHp"],
    )
    retries = int(ua_cfg.get("retries", 5))
    retry_delay = float(ua_cfg.get("retry_delay_sec", 3.0))

    log.info("Starting PZEM → OPC UA writer")
    log.info("Connecting to OPC UA: %s", endpoint)

    client = Client(url=endpoint)
    writer: Optional[UmatiPrWriter] = None

    # Connection & node resolution with simple retry
    for attempt in range(1, retries + 1):
        try:
            await client.connect()
            log.info("Connected.")
            writer = UmatiPrWriter(
                client=client,
                machine_root_id=ua.NodeId.from_string(machine_root),
                channel_path=channel_path,
                variable_names=var_names,
                verbose=verbose,
            )
            if not await writer.resolve_nodes():
                raise RuntimeError("Node resolution failed.")
            break
        except Exception as e:
            logging.warning("Connect/resolve failed (attempt %d/%d): %s", attempt, retries, e)
            try:
                await client.disconnect()
            except Exception:
                pass
            writer = None
            if attempt < retries:
                await asyncio.sleep(retry_delay)
            else:
                # Give up after the last attempt
                return

    sample = float(cfg.get("timing", {}).get("sample_interval_sec", 5.0))
    tick = 0

    try:
        while True:
            # 1) Read raw
            r = pzem.read()
            if r.status not in ("OK", "SIM"):
                logging.warning("PZEM status: %s", r.status)

            # 2) Scale to engineering units expected by the model
            v = float(r.voltage) * v_scale
            i = float(r.current) * i_scale
            p = float(r.power) * p_scale
            e = float(r.energy) * e_scale

            # 3) Prepare payload by browseName
            payload = {
                "AcVoltagePe": v,
                "AcCurrentPe": i,
                "AcActivePowerPe": p,
                "AcActiveEnergyTotalImportHp": e,
            }

            # 4) Write to OPC UA
            if writer and not await writer.write_values(payload):
                logging.warning("Write returned not ok")

            # Optional: periodic value logs (DEBUG only when enabled)
            tick += 1
            if log_values and (tick % max(1, values_every_n) == 0):
                logging.debug("V=%.1f V, I=%.3f A, P=%.1f W, E=%.3f Wh", v, i, p, e)

            await asyncio.sleep(sample)

    except asyncio.CancelledError:
        # Soft cancellation (systemd/compose stop) without stack traces
        pass
    except KeyboardInterrupt:
        # Manual stop (Ctrl+C)
        log.info("Stopping (Ctrl+C)…")
    finally:
        # Always attempt a clean disconnect
        try:
            await client.disconnect()
            log.info("Disconnected.")
        except Exception:
            pass


if __name__ == "__main__":
    # uvloop is optional; improves event loop performance on Linux
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except Exception:
        pass

    # Suppress asyncio's final KeyboardInterrupt stack trace
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
