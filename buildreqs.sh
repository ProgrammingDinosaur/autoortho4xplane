#!/bin/bash
set -eo pipefail  # Exit on errors, including failed downloads in pipelines

export DEBIAN_FRONTEND=noninteractive
export TZ=America/New_York

#apt-get update
#apt-get install software-properties-common -y
#add-apt-repository ppa:deadsnakes/ppa -y
apt-get update
#apt-get install -y make curl patchelf python3.10 python3.10-tk zlib1g-dev \
#    ccache python3.10-distutils python3.10-dev libjpeg-dev libturbojpeg0-dev build-essential
apt-get install -y make curl ca-certificates patchelf python3-pip python3-venv python3-tk zlib1g-dev \
    ccache python3-setuptools python3-dev libjpeg-dev libturbojpeg0-dev build-essential \
    libglib2.0-0 libx11-6 libxcb1 libxkbcommon0 libdbus-1-3 libfontconfig1 libfreetype6 \
    libgl1 libegl1

# Always install Python 3.14 if not already 3.14+
# We need Python 3.14 for _zstd support and consistent builds
if python3 -c 'import sys; exit(0) if sys.version_info < (3, 14) else exit(1)'; then
    echo "Installing Python 3.14 with pyenv (current: $(python3 --version))..."
    apt-get install -y git libssl-dev \
        libbz2-dev libreadline-dev libsqlite3-dev \
        libncursesw5-dev tk-dev libxml2-dev \
        libxmlsec1-dev libffi-dev liblzma-dev libzstd-dev pkg-config
    export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
    # Install directly from GitHub to avoid depending on the pyenv.run hostname.
    if [[ ! -x "$PYENV_ROOT/bin/pyenv" ]]; then
        git clone --depth 1 https://github.com/pyenv/pyenv.git "$PYENV_ROOT"
    fi
    export PATH="$PYENV_ROOT/bin:$PATH"
    # Retry transient failures when python-build downloads the Python sources.
    export PYTHON_BUILD_CURL_OPTS="--retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 30"
    pyenv install -s 3.14.2
    # Select the interpreter explicitly for this noninteractive build script.
    export PATH="$PYENV_ROOT/versions/3.14.2/bin:$PATH"
    hash -r
fi

echo "Using Python version: $(python3 --version)"

# Verify we have Python 3.14+
if python3 -c 'import sys; exit(0) if sys.version_info >= (3, 14) else exit(1)'; then
    echo "Python 3.14+ confirmed."
else
    echo "ERROR: Python 3.14+ is required but not available!"
    echo "Current Python: $(python3 --version)"
    exit 1
fi

# A source build can succeed without optional modules when headers are missing.
python3 -c 'import compression.zstd'

# Create and prepare an isolated virtual environment to avoid PEP 668 restrictions
echo "Creating virtual environment..."
python3 -m venv .venv
. .venv/bin/activate
echo "Upgrading pip and setuptools..."
pip install -U pip setuptools
echo "Installing build requirements (including pyinstaller)..."
pip install -r requirements-build.txt
echo "Installing runtime requirements..."
pip install -r requirements.txt
echo "Build environment setup complete!"
