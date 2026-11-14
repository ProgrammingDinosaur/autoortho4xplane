# AutoOrtho X-Plane plugin (files mode)

`ao_gate.c` is the whole plugin. In files mode it:

- holds X-Plane's loading screen at a flight start until AutoOrtho has built the textures around
  the start (UDP on 127.0.0.1, protocol in `autoortho/files_mode/gate.py`);
- removes AutoOrtho's stale files-mode links from Custom Scenery when X-Plane starts without
  AutoOrtho (only links resolving under the files folder named in `autoortho_gate.txt`).

It looks up XPLM functions at run time, so no X-Plane SDK is needed to build it.

| Platform | Build | Output |
|---|---|---|
| Windows | `build_win.bat` (MSVC) or `make mingw` | `bin/win_x64/AutoOrtho.xpl` |
| macOS | `make mac` (universal, ad-hoc signed) | `bin/mac_x64/AutoOrtho.xpl` |
| Linux | `make linux` | `bin/lin_x64/AutoOrtho.xpl` |

The files worker copies the binary to `X-Plane 12/Resources/plugins/AutoOrtho/<platform>/` when it
differs, and writes `autoortho_gate.txt` (gate port, files root, Custom Scenery path) next to it.
