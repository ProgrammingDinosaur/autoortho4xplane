#!/bin/bash
set -e  # Exit on any error

export DEBIAN_FRONTEND=noninteractive
export TZ=America/New_York

# =============================================================================
# PYTHON 3.14 FREE-THREADING BUILD CONFIGURATION
# =============================================================================
# Python 3.14+ with free-threading (no-GIL) requires:
# - Installing Python with the free-threading build (python3.14t)
# - Using the 't' suffix indicates the free-threading variant
# - Set PYTHON_GIL=0 or use -X gil=0 flag to enable free-threading at runtime
# =============================================================================

# Target Python version for free-threading support
PYTHON_VERSION="3.14"
PYTHON_FULL_VERSION="3.14.0"

apt-get update
apt-get install -y make curl patchelf python3-pip python3-venv python3-tk zlib1g-dev \
    ccache python3-setuptools python3-dev libjpeg-dev libturbojpeg0-dev build-essential \
    libglib2.0-0 libx11-6 libxcb1 libxkbcommon0 libdbus-1-3 libfontconfig1 libfreetype6 \
    libgl1 libegl1

# Function to check if free-threading Python is available
check_freethreading_python() {
    if command -v python${PYTHON_VERSION}t &> /dev/null; then
        echo "Found free-threading Python: python${PYTHON_VERSION}t"
        return 0
    elif command -v python${PYTHON_VERSION} &> /dev/null; then
        # Check if it's built with free-threading support
        if python${PYTHON_VERSION} -c "import sys; sys.exit(0 if hasattr(sys, '_is_gil_enabled') else 1)" 2>/dev/null; then
            echo "Found Python ${PYTHON_VERSION} with free-threading support"
            return 0
        fi
    fi
    return 1
}

# Install Python 3.14 with free-threading if not available
if ! check_freethreading_python; then
    echo "Installing Python ${PYTHON_VERSION} with free-threading support using pyenv..."
    
    # Install pyenv dependencies
    apt-get install -y git libssl-dev \
        libbz2-dev libreadline-dev libsqlite3-dev \
        libncursesw5-dev tk-dev libxml2-dev \
        libxmlsec1-dev libffi-dev liblzma-dev
    
    # Install pyenv if not present
    if [ ! -d "$HOME/.pyenv" ]; then
        curl https://pyenv.run | bash
    fi
    
    export PYENV_ROOT="$HOME/.pyenv"
    [[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init - bash)"
    
    # Install Python 3.14 with free-threading (the 't' suffix)
    # pyenv uses the suffix 't' for free-threading builds
    if pyenv install --list | grep -q "${PYTHON_FULL_VERSION}t"; then
        echo "Installing Python ${PYTHON_FULL_VERSION}t (free-threading)..."
        pyenv install ${PYTHON_FULL_VERSION}t
        pyenv global ${PYTHON_FULL_VERSION}t
    elif pyenv install --list | grep -q "${PYTHON_FULL_VERSION}"; then
        echo "Free-threading build not available in pyenv, installing standard ${PYTHON_FULL_VERSION}..."
        echo "Note: Free-threading optimizations will not be available."
        pyenv install ${PYTHON_FULL_VERSION}
        pyenv global ${PYTHON_FULL_VERSION}
    else
        echo "Python ${PYTHON_VERSION} not available in pyenv. Trying latest available 3.14.x..."
        LATEST_314=$(pyenv install --list | grep -E "^\s*3\.14\.[0-9]+t?$" | tail -1 | tr -d ' ')
        if [ -n "$LATEST_314" ]; then
            pyenv install $LATEST_314
            pyenv global $LATEST_314
        else
            echo "ERROR: Could not find Python 3.14.x in pyenv"
            exit 1
        fi
    fi
fi

# Determine which python command to use
if command -v python${PYTHON_VERSION}t &> /dev/null; then
    PYTHON_CMD="python${PYTHON_VERSION}t"
elif command -v python${PYTHON_VERSION} &> /dev/null; then
    PYTHON_CMD="python${PYTHON_VERSION}"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
else
    echo "ERROR: No suitable Python found"
    exit 1
fi

echo "Using Python: $PYTHON_CMD"
$PYTHON_CMD --version

# Check if free-threading is available
if $PYTHON_CMD -c "import sys; print('Free-threading:', 'enabled' if hasattr(sys, '_is_gil_enabled') and not sys._is_gil_enabled() else 'available' if hasattr(sys, '_is_gil_enabled') else 'not available')" 2>/dev/null; then
    :
fi

# Create and prepare an isolated virtual environment to avoid PEP 668 restrictions
echo "Creating virtual environment..."
$PYTHON_CMD -m venv .venv
. .venv/bin/activate

echo "Upgrading pip and setuptools..."
pip install -U pip setuptools

echo "Installing build requirements (including pyinstaller)..."
pip install -r requirements-build.txt

echo "Installing runtime requirements..."
pip install -r requirements.txt

echo ""
echo "============================================"
echo "Build environment setup complete!"
echo "Python: $(python --version)"
if python -c "import sys; sys.exit(0 if hasattr(sys, '_is_gil_enabled') else 1)" 2>/dev/null; then
    echo "Free-threading: AVAILABLE"
    echo ""
    echo "To run with free-threading enabled:"
    echo "  PYTHON_GIL=0 python -m autoortho"
    echo "  or"
    echo "  python -X gil=0 -m autoortho"
else
    echo "Free-threading: NOT AVAILABLE (standard Python build)"
fi
echo "============================================"
