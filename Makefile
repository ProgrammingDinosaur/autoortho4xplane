ZIP?=zip
VERSION?=0.0.0
# Sanitize VERSION for use in filenames (replace any non-safe char with '-')
SAFE_VERSION:=$(shell echo "$(VERSION)" | sed -e 's/[^A-Za-z0-9._-]/-/g')
.PHONY: mac_app
SHELL := /bin/bash
.ONESHELL:

autoortho.pyz:
	mkdir -p build/autoortho
	cp -r autoortho/* build/autoortho/.
	python3 -m pip install -U -r ./build/autoortho/build-reqs.txt --target ./build/autoortho
	cd build && python3 -m zipapp -p "/usr/bin/env python3" autoortho

lin_bin: autoortho_lin_$(SAFE_VERSION).bin
autoortho_lin_$(SAFE_VERSION).bin: autoortho/*.py
	docker run --rm -v `pwd`:/code ubuntu:noble /bin/bash -c "cd /code; ./buildreqs.sh; . .venv/bin/activate; time make bin VERSION=$(VERSION)"
	mv autoortho_lin.bin $@

enter:
	docker run --rm -it -v `pwd`:/code ubuntu:focal /bin/bash

autoortho/.version:
	echo "$(VERSION)" > $@

bin: autoortho/.version
	# Ensure required Linux libraries and helper binaries are executable before packaging
	chmod +x autoortho/lib/linux/7zip/7zz || true
	chmod +x autoortho/lib/linux/DSFTool || true
	chmod +x autoortho/lib/linux/*.so || true
	chmod +x autoortho/aoimage/aoimage.so || true
	python3 -m nuitka --verbose --verbose-output=nuitka.log \
		--linux-icon=autoortho/imgs/ao-icon.ico \
		--enable-plugin=pyside6 \
		--include-data-file=./autoortho/.version*=. \
		--include-data-file=./autoortho/templates/*.html=templates/ \
		--onefile \
		--user-package-configuration-file=nuitka-package.config.yml \
		./autoortho/__main__.py -o autoortho_lin.bin

__main__.app: autoortho/.version
	# Ensure required macOS libraries and helper binaries are executable before packaging
	chmod +x autoortho/lib/macos/7zip/7zz || true
	chmod +x autoortho/lib/macos/DSFTool || true
	chmod +x autoortho/lib/macos/*.dylib || true
	chmod +x autoortho/aoimage/aoimage.dylib || true
	sudo python3 -m nuitka --verbose --verbose-output=nuitka.log \
		--assume-yes-for-downloads \
		--standalone \
		--macos-create-app-bundle \
		--macos-target-arch=arm64 \
		--macos-app-name=AutoOrtho \
		--macos-app-icon=autoortho/imgs/ao-icon.icns \
		--enable-plugin=pyside6 \
		--include-data-file=autoortho/.version=autoortho/.version \
		--include-data-file=./autoortho/templates/*.html=templates/ \
		--user-package-configuration-file=nuitka-package.config.yml \
		./autoortho/__main__.py

AutoOrtho.app: __main__.app
	sudo chown -R "$(USER)":staff __main__.app
	rm -rf AutoOrtho.app
	mv __main__.app AutoOrtho.app

mac_app: AutoOrtho.app

AutoOrtho_mac_$(SAFE_VERSION).zip: AutoOrtho.app
	$(ZIP) -r $@ AutoOrtho.app
mac_zip: AutoOrtho_mac_$(SAFE_VERSION).zip

_autoortho_win.exe: autoortho/.version
	python3 -m nuitka --verbose --verbose-output=nuitka.log \
		--mingw64 \
		--disable-ccache \
		--enable-plugin=pyside6 \
		--windows-icon-from-ico=autoortho/imgs/ao-icon.ico \
		--assume-yes-for-downloads \
		--include-data-file=./autoortho/.version*=./ \
		--include-data-file=./autoortho/templates/*.html=templates/ \
		--user-package-configuration-file=nuitka-package.config.yml \
		--onefile \
		--disable-console \
		./autoortho/__main__.py -o autoortho_win.exe

__main__.dist: autoortho/.version
	python3 -m nuitka --verbose --verbose-output=nuitka.log \
		--mingw64 \
		--disable-ccache \
		--enable-plugin=pyside6 \
		--windows-icon-from-ico=autoortho/imgs/ao-icon.ico \
		--assume-yes-for-downloads \
		--include-data-file=./autoortho/.version*=./ \
		--include-data-file=./autoortho/templates/*.html=templates/ \
		--user-package-configuration-file=nuitka-package.config.yml \
		--standalone \
		--disable-console \
		./autoortho/__main__.py -o autoortho_win.exe

win_exe: AutoOrtho_win_$(SAFE_VERSION).exe
AutoOrtho_win_$(SAFE_VERSION).exe: __main__.dist
	cp autoortho/imgs/ao-icon.ico .
	makensis -DPRODUCT_VERSION=$(VERSION) installer.nsi
	mv AutoOrtho.exe $@

win_zip: autoortho_win_$(SAFE_VERSION).zip
autoortho_win_$(SAFE_VERSION).zip: __main__.dist
	mv __main__.dist autoortho_release
	$(ZIP) $@ autoortho_release

testperf:
	python3 -m nuitka --verbose --verbose-output=nuitka.log  --include-data-dir=./autoortho/lib=lib --include-data-dir=./autoortho/testfiles=testfiles --onefile ./autoortho/perftest.py

%.txt: %.in
	pip-compile $<

serve_docs:
	docker run -p 8000:8000 -v `pwd`:/docs squidfunk/mkdocs-material

clean:
	rm -rf __main__.app AutoOrtho.app *.zip *.build *.dist build dist
	
