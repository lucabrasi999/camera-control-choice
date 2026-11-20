# Camera Control Choice

A small desktop app for the ZWO ASI533MC Pro that lets you choose where the camera is run from:

- **This laptop (USB)**: uses the ZWO ASI SDK through the `zwoasi` Python package. Images are saved on the laptop as FITS files.
- **ASIAir (network)**: the camera is plugged into the ASIAir and the laptop acts as a remote control. The ASIAir saves the images on its own storage.

## Run it

```bash
pip install zwoasi numpy astropy
python3 asi533_capture.py
```

For laptop mode, download the ASI Camera SDK for macOS from https://www.zwoastro.com/software/ and enter the path to `libASICamera2.dylib` in the app.

## Status

- Laptop mode has not yet been tested with a real camera.
- ASIAir mode uses ZWO's unofficial network protocol. The command names in `ASIAIR_METHODS` (top of `asi533_capture.py`) are unverified and may need adjusting for your ASIAir firmware.
- A camera can only be controlled from one place at a time.
