#!/usr/bin/env python3
"""
ASI533MC Pro capture app: run the camera from this laptop OR from an ASIAir.

Pick the source with the radio buttons at the top.

  Laptop : camera is plugged into this computer by USB. Uses the ZWO ASI SDK
           through the `zwoasi` Python package. Images are saved here.
  ASIAir : camera is plugged into the ASIAir. This computer talks to the ASIAir
           over the network (its own WiFi or your home network). The ASIAir
           saves the images to its own storage.

Setup (laptop mode):
    pip install zwoasi numpy astropy
    Download the ASI Camera SDK for macOS from https://www.zwoastro.com/software/
    and point the "SDK library" box at libASICamera2.dylib.

NOTE: ZWO publishes no official API for the ASIAir. The ASIAir backend below
uses its unofficial JSON-RPC port (4700) and the method names in ASIAIR_METHODS,
which come from community reverse engineering and have NOT been verified against
your firmware. If a command fails, the log shows the ASIAir's reply; adjust
ASIAIR_METHODS to match.
"""
import json
import queue
import socket
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk

# ---------------------------------------------------------------- ASIAir config
ASIAIR_RPC_PORT = 4700
ASIAIR_DISCOVERY_UDP_PORT = 4720
ASIAIR_METHODS = {
    "get_state": "get_camera_state",
    "set_gain": "set_control_value",        # params: ["gain", value]
    "set_exposure": "set_control_value",    # params: ["exposure", microseconds]
    "start": "start_exposure",              # params: ["light"]
    "abort": "stop_exposure",
}


# ------------------------------------------------------------------- backends
class CameraError(Exception):
    pass


class LocalCamera:
    """ASI533MC Pro attached to this laptop via the ZWO ASI SDK."""

    name = "Laptop (USB)"

    def __init__(self, sdk_path: str, save_dir: Path):
        self.sdk_path, self.save_dir = sdk_path, save_dir
        self.cam = None
        self.asi = None

    def connect(self):
        try:
            import zwoasi as asi
        except ImportError as e:
            raise CameraError("Missing package: pip install zwoasi numpy astropy") from e
        if not self.sdk_path:
            raise CameraError("Set the path to libASICamera2.dylib (ZWO ASI SDK).")
        try:
            asi.init(self.sdk_path)
        except Exception as e:  # zwoasi raises if already initialised; that's fine
            if "already" not in str(e).lower():
                raise CameraError(f"SDK init failed: {e}") from e
        if asi.get_num_cameras() == 0:
            raise CameraError("No ZWO camera found on USB. Is it plugged into this laptop "
                              "(and not into the ASIAir)?")
        self.asi = asi
        self.cam = asi.Camera(0)
        info = self.cam.get_camera_property()
        self.cam.set_image_type(asi.ASI_IMG_RAW16)
        return f"{info['Name']} {info['MaxWidth']}x{info['MaxHeight']}"

    def configure(self, gain, exposure_s, cooler_on, target_c):
        a = self.asi
        self.cam.set_control_value(a.ASI_GAIN, int(gain))
        self.cam.set_control_value(a.ASI_EXPOSURE, int(exposure_s * 1_000_000))
        controls = self.cam.get_controls()
        if "CoolerOn" in controls:
            self.cam.set_control_value(a.ASI_COOLER_ON, 1 if cooler_on else 0)
            if cooler_on:
                self.cam.set_control_value(a.ASI_TARGET_TEMP, int(target_c))

    def status(self):
        a = self.asi
        t = self.cam.get_control_value(a.ASI_TEMPERATURE)[0] / 10.0
        controls = self.cam.get_controls()
        s = f"Sensor {t:.1f} C"
        if "CoolerPowerPerc" in controls:
            s += f", cooler {self.cam.get_control_value(a.ASI_COOLER_POWER_PERC)[0]}%"
        return s

    def capture(self, exposure_s):
        import numpy as np
        data = self.cam.capture()          # blocks until exposure finishes
        arr = np.asarray(data)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            from astropy.io import fits
            path = self.save_dir / f"ASI533MC_{stamp}.fits"
            hdr = fits.Header()
            hdr["EXPTIME"], hdr["INSTRUME"], hdr["BAYERPAT"] = exposure_s, "ZWO ASI533MC Pro", "RGGB"
            fits.PrimaryHDU(arr, header=hdr).writeto(path)
        except ImportError:
            path = self.save_dir / f"ASI533MC_{stamp}.npy"
            np.save(path, arr)
        return f"Saved {path}"

    def abort(self):
        try:
            self.cam.stop_exposure()
        except Exception:
            pass

    def close(self):
        try:
            if self.cam:
                self.cam.close()
        except Exception:
            pass


class ASIAirCamera:
    """ASI533MC Pro attached to an ASIAir; this laptop is a remote control."""

    name = "ASIAir (network)"

    def __init__(self, host: str):
        self.host, self.sock, self._id = host, None, 0

    @staticmethod
    def discover(timeout=3.0):
        """Broadcast a UDP probe; return list of responding IPs (best effort)."""
        found = set()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.5)
        try:
            s.sendto(b"scan_air", ("255.255.255.255", ASIAIR_DISCOVERY_UDP_PORT))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    _, addr = s.recvfrom(2048)
                    found.add(addr[0])
                except socket.timeout:
                    pass
        finally:
            s.close()
        return sorted(found)

    def _rpc(self, method, params=None):
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.sock.sendall((json.dumps(msg) + "\r\n").encode())
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CameraError("ASIAir closed the connection")
            buf += chunk
        try:
            return json.loads(buf.decode().strip().splitlines()[0])
        except (ValueError, IndexError):
            return {"raw": buf.decode(errors="replace")}

    def connect(self):
        if not self.host:
            raise CameraError("Enter the ASIAir IP address or press Find.")
        try:
            self.sock = socket.create_connection((self.host, ASIAIR_RPC_PORT), timeout=5)
        except OSError as e:
            raise CameraError(f"Cannot reach ASIAir at {self.host}:{ASIAIR_RPC_PORT} ({e}). "
                              "Is this laptop on the ASIAir's WiFi, and is the ASIAir camera "
                              "connected in the ASIAir app?") from e
        self.sock.settimeout(10)
        return f"ASIAir {self.host}: {self._rpc(ASIAIR_METHODS['get_state'])}"

    def configure(self, gain, exposure_s, cooler_on, target_c):
        m = ASIAIR_METHODS
        self._rpc(m["set_gain"], ["gain", int(gain)])
        self._rpc(m["set_exposure"], ["exposure", int(exposure_s * 1_000_000)])
        # Cooler control over the unofficial API is not mapped; set it in the ASIAir app.

    def status(self):
        return str(self._rpc(ASIAIR_METHODS["get_state"]))

    def capture(self, exposure_s):
        reply = self._rpc(ASIAIR_METHODS["start"], ["light"])
        return f"ASIAir replied {reply}. Image is saved on the ASIAir, not this laptop."

    def abort(self):
        try:
            self._rpc(ASIAIR_METHODS["abort"])
        except Exception:
            pass

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------------------ GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ASI533MC Pro Capture")
        self.geometry("560x560")
        self.backend = None
        self.busy = False
        self.logq = queue.Queue()

        self.source = tk.StringVar(value="laptop")
        self.sdk = tk.StringVar(value="/usr/local/lib/libASICamera2.dylib")
        self.savedir = tk.StringVar(value=str(Path.home() / "Pictures" / "ASI533"))
        self.air_ip = tk.StringVar()
        self.gain = tk.IntVar(value=100)
        self.exposure = tk.DoubleVar(value=10.0)
        self.cooler = tk.BooleanVar(value=True)
        self.target = tk.IntVar(value=-10)

        self._build()
        self._on_source()
        self.after(100, self._drain_log)

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        src = ttk.LabelFrame(self, text="Run camera from")
        src.pack(fill="x", **pad)
        ttk.Radiobutton(src, text="This laptop (USB)", value="laptop",
                        variable=self.source, command=self._on_source).pack(side="left", **pad)
        ttk.Radiobutton(src, text="ASIAir (network)", value="asiair",
                        variable=self.source, command=self._on_source).pack(side="left", **pad)

        self.laptop_f = ttk.LabelFrame(self, text="Laptop settings")
        ttk.Label(self.laptop_f, text="SDK library").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(self.laptop_f, textvariable=self.sdk, width=42).grid(row=0, column=1, **pad)
        ttk.Button(self.laptop_f, text="Browse", command=lambda: self._pick(self.sdk, False)
                   ).grid(row=0, column=2, **pad)
        ttk.Label(self.laptop_f, text="Save to").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(self.laptop_f, textvariable=self.savedir, width=42).grid(row=1, column=1, **pad)
        ttk.Button(self.laptop_f, text="Browse", command=lambda: self._pick(self.savedir, True)
                   ).grid(row=1, column=2, **pad)

        self.air_f = ttk.LabelFrame(self, text="ASIAir settings")
        ttk.Label(self.air_f, text="ASIAir IP").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(self.air_f, textvariable=self.air_ip, width=24).grid(row=0, column=1, **pad)
        ttk.Button(self.air_f, text="Find", command=self._find_air).grid(row=0, column=2, **pad)
        ttk.Label(self.air_f, text="Tip: when joined to the ASIAir's own WiFi it is usually "
                  "172.20.xxx.1; check the ASIAir app.", wraplength=480, foreground="gray"
                  ).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        cam = ttk.LabelFrame(self, text="Exposure")
        cam.pack(fill="x", **pad)
        ttk.Label(cam, text="Gain (0-600)").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(cam, from_=0, to=600, textvariable=self.gain, width=8).grid(row=0, column=1, **pad)
        ttk.Label(cam, text="Exposure (s)").grid(row=0, column=2, sticky="w", **pad)
        ttk.Spinbox(cam, from_=0.001, to=600, increment=1, textvariable=self.exposure,
                    width=8).grid(row=0, column=3, **pad)
        ttk.Checkbutton(cam, text="Cooler on", variable=self.cooler).grid(row=1, column=0, **pad)
        ttk.Label(cam, text="Target (C)").grid(row=1, column=2, sticky="w", **pad)
        ttk.Spinbox(cam, from_=-30, to=30, textvariable=self.target, width=8).grid(row=1, column=3, **pad)

        row = ttk.Frame(self)
        row.pack(fill="x", **pad)
        self.btn_connect = ttk.Button(row, text="Connect", command=self._connect)
        self.btn_connect.pack(side="left", **pad)
        self.btn_capture = ttk.Button(row, text="Capture", command=self._capture, state="disabled")
        self.btn_capture.pack(side="left", **pad)
        self.btn_abort = ttk.Button(row, text="Abort", command=self._abort, state="disabled")
        self.btn_abort.pack(side="left", **pad)
        self.btn_status = ttk.Button(row, text="Status", command=self._status, state="disabled")
        self.btn_status.pack(side="left", **pad)

        self.logbox = tk.Text(self, height=14, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True, **pad)

    # -- helpers
    def log(self, msg):
        self.logq.put(f"[{datetime.now():%H:%M:%S}] {msg}")

    def _drain_log(self):
        while not self.logq.empty():
            self.logbox.config(state="normal")
            self.logbox.insert("end", self.logq.get() + "\n")
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.after(100, self._drain_log)

    def _pick(self, var, directory):
        p = filedialog.askdirectory() if directory else filedialog.askopenfilename()
        if p:
            var.set(p)

    def _on_source(self):
        self._disconnect()
        self.laptop_f.pack_forget()
        self.air_f.pack_forget()
        frame = self.laptop_f if self.source.get() == "laptop" else self.air_f
        frame.pack(fill="x", padx=8, pady=4, after=self.winfo_children()[0])

    def _find_air(self):
        self.log("Searching for ASIAir...")

        def work():
            ips = ASIAirCamera.discover()
            if ips:
                self.air_ip.set(ips[0])
                self.log(f"Found: {', '.join(ips)}")
            else:
                self.log("No ASIAir answered. Enter its IP manually.")
        threading.Thread(target=work, daemon=True).start()

    def _disconnect(self):
        if self.backend:
            self.backend.close()
            self.backend = None
        if hasattr(self, "btn_capture"):
            for b in (self.btn_capture, self.btn_abort, self.btn_status):
                b.config(state="disabled")
            self.btn_connect.config(state="normal")

    def _run(self, fn, done=None):
        if self.busy:
            return
        self.busy = True

        def work():
            try:
                fn()
            except Exception as e:
                self.log(f"ERROR: {e}")
            finally:
                self.busy = False
                if done:
                    self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # -- actions
    def _connect(self):
        if self.source.get() == "laptop":
            be = LocalCamera(self.sdk.get().strip(), Path(self.savedir.get()))
        else:
            be = ASIAirCamera(self.air_ip.get().strip())

        def work():
            self.log(f"Connecting via {be.name}...")
            self.log(f"Connected: {be.connect()}")
            self.backend = be

        def done():
            if self.backend:
                self.btn_connect.config(state="disabled")
                for b in (self.btn_capture, self.btn_abort, self.btn_status):
                    b.config(state="normal")
        self._run(work, done)

    def _capture(self):
        be = self.backend
        exp = float(self.exposure.get())

        def work():
            be.configure(self.gain.get(), exp, self.cooler.get(), self.target.get())
            self.log(f"Exposing {exp}s at gain {self.gain.get()}...")
            self.log(be.capture(exp))
        self._run(work)

    def _abort(self):
        if self.backend:
            self.backend.abort()
            self.log("Abort sent.")

    def _status(self):
        be = self.backend
        self._run(lambda: self.log(be.status()))


if __name__ == "__main__":
    App().mainloop()
