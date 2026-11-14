# AutoOrtho X-Plane plugin (files mode). No X-Plane SDK needed: XPLM
# functions are looked up at run time.
#
#   make linux     -> bin/lin_x64/AutoOrtho.xpl
#   make mac       -> bin/mac_x64/AutoOrtho.xpl (universal arm64 + x86_64, ad-hoc signed)
#   make mingw     -> bin/win_x64/AutoOrtho.xpl (cross-compile; MSVC: build_win.bat)

SRC := ao_gate.c

linux:
	mkdir -p bin/lin_x64
	$(CC) -O2 -Wall -fPIC -shared -fvisibility=hidden -o bin/lin_x64/AutoOrtho.xpl $(SRC) -ldl
	-python3 stamp.py

mac:
	mkdir -p bin/mac_x64
	clang -O2 -Wall -fPIC -shared -fvisibility=hidden -arch arm64 -arch x86_64 \
	  -mmacosx-version-min=10.15 -undefined dynamic_lookup \
	  -o bin/mac_x64/AutoOrtho.xpl $(SRC)
	codesign --force --sign - bin/mac_x64/AutoOrtho.xpl
	-python3 stamp.py

mingw:
	mkdir -p bin/win_x64
	x86_64-w64-mingw32-gcc -O2 -Wall -shared -static-libgcc -o bin/win_x64/AutoOrtho.xpl \
	  $(SRC) -lws2_32
	-python3 stamp.py

.PHONY: linux mac mingw
