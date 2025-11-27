#!/bin/bash
set -e  # Exit on any error

export DEBIAN_FRONTEND=noninteractive
export TZ=America/New_York

#apt-get update
#apt-get install software-properties-common -y
#add-apt-repository ppa:deadsnakes/ppa -y
apt-get update
#apt-get install -y make curl patchelf python3.10 python3.10-tk zlib1g-dev \
#    ccache python3.10-distutils python3.10-dev libjpeg-dev libturbojpeg0-dev build-essential
apt-get install -y make curl patchelf python3-pip python3-venv python3-tk zlib1g-dev \
    ccache python3-setuptools python3-dev libjpeg-dev libturbojpeg0-dev build-essential \
    libglib2.0-0 libx11-6 libxcb1 libxkbcommon0 libdbus-1-3 libfontconfig1 libfreetype6 \
    libgl1 libegl1

if python3 -c 'import sys; exit(0) if sys.version_info.minor < 12 else exit(1)'; then
    echo "Installing Python 3.12 with pyenv..."
    apt-get install -y git libssl-dev \
        libbz2-dev libreadline-dev libsqlite3-dev \
        libncursesw5-dev tk-dev libxml2-dev \
        libxmlsec1-dev libffi-dev liblzma-dev
    curl https://pyenv.run | bash
    export PYENV_ROOT="$HOME/.pyenv"
    [[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init - bash)"
    pyenv install 3.12.3
    pyenv global 3.12.3
fi
echo "Using Python version: "
python3 --version

# Create and prepare an isolated virtual environment to avoid PEP 668 restrictions
echo "Creating virtual environment..."
python3 -m venv .venv
. .venv/bin/activate
echo "Upgrading pip and setuptools..."
pip install -U pip setuptools
echo "Installing build requirements (including nuitka)..."
pip install -r requirements-build.txt
echo "Installing runtime requirements..."
pip install -r requirements.txt
echo "Build environment setup complete!"
