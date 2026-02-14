"""
7-Zip wrapper module using the bundled 7zz/7za binary.

This replaces py7zr for extracting DSF files from 7z archives,
avoiding the _zstd dependency issues on Linux with Python 3.14+.
"""

import os
import sys
import io
import re
import subprocess
from typing import Optional, List, Dict, Any

import logging
log = logging.getLogger(__name__)

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.utils.constants import system_type
except ImportError:
    from utils.constants import system_type


class SevenZipFileInfo:
    """Info about a file inside a 7z archive."""
    
    def __init__(self, name: str, size: int = 0, compressed: int = 0):
        self.name = name
        self.size = size  # uncompressed size
        self.uncompressed = size
        self.uncompressed_size = size
        self.compressed = compressed


class SevenZipError(Exception):
    """Error from 7-Zip operations."""
    pass


def get_7zip_binary() -> str:
    """Get the path to the bundled 7-Zip binary."""
    if system_type == "windows":
        lib_subpath = os.path.join("windows", "7zip", "7za.exe")
    elif system_type == "linux":
        lib_subpath = os.path.join("linux", "7zip", "7zz")
    elif system_type == "darwin":
        lib_subpath = os.path.join("macos", "7zip", "7zz")
    else:
        raise SevenZipError(f"Unsupported system type: {system_type}")
    
    # Handle PyInstaller frozen mode
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        base_dir = os.path.join(sys._MEIPASS, 'autoortho')
    else:
        # Development mode - go up from utils/ to autoortho/
        base_dir = os.path.dirname(os.path.dirname(__file__))
    
    binary_path = os.path.join(base_dir, "lib", lib_subpath)
    
    if not os.path.isfile(binary_path):
        raise SevenZipError(f"7-Zip binary not found at: {binary_path}")
    
    return binary_path


class SevenZipFile:
    """
    Context manager for reading 7z archives using the bundled 7zz/7za binary.
    
    This provides a py7zr-compatible interface for the operations used in aoseasons.py.
    
    Usage:
        with SevenZipFile(archive_path, mode='r') as archive:
            names = archive.getnames()
            info = archive.getinfo(name)
            data = archive.extract_to_bytes(name)
    """
    
    def __init__(self, archive_path: str, mode: str = 'r'):
        if mode != 'r':
            raise SevenZipError("Only read mode ('r') is supported")
        
        self.archive_path = archive_path
        self._binary = get_7zip_binary()
        self._file_list: Optional[List[str]] = None
        self._file_info: Optional[Dict[str, SevenZipFileInfo]] = None
        
        if not os.path.isfile(archive_path):
            raise FileNotFoundError(f"Archive not found: {archive_path}")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass  # Nothing to clean up for read-only access
    
    def _run_7zip(self, args: List[str], capture_stdout: bool = True) -> subprocess.CompletedProcess:
        """Run 7-Zip with the given arguments."""
        cmd = [self._binary] + args
        
        # Windows-specific: hide console window
        creationflags = 0
        startupinfo = None
        if system_type == "windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = 0  # SW_HIDE
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            return result
        except OSError as e:
            raise SevenZipError(f"Failed to execute 7-Zip: {e}")
    
    def _parse_listing(self) -> None:
        """Parse 7-Zip listing output to get file names and sizes."""
        if self._file_list is not None:
            return  # Already parsed
        
        # Use technical listing format for reliable parsing
        result = self._run_7zip(['l', '-slt', self.archive_path])
        
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='ignore')
            raise SevenZipError(f"Failed to list archive: {stderr}")
        
        output = result.stdout.decode(errors='replace')
        
        self._file_list = []
        self._file_info = {}
        
        # Parse technical listing format
        # The output has a header section (archive info) followed by file entries
        # File entries are separated by "----------" lines
        # We need to skip the header and only parse actual file entries
        
        in_file_section = False
        current_path = None
        current_size = 0
        current_compressed = 0
        
        for line in output.splitlines():
            line = line.strip()
            
            # "----------" marks the boundary between sections
            # First one: end of header, start of file entries
            # Subsequent ones: between file entries
            if line.startswith('----------'):
                # Save previous file if we were tracking one
                if current_path is not None:
                    self._file_list.append(current_path)
                    self._file_info[current_path] = SevenZipFileInfo(
                        name=current_path,
                        size=current_size,
                        compressed=current_compressed
                    )
                    current_path = None
                
                in_file_section = True
                continue
            
            # Only process lines after we've seen the first separator
            if not in_file_section:
                continue
            
            if line.startswith('Path = '):
                # Save previous file if any (shouldn't happen with proper separator handling)
                if current_path is not None:
                    self._file_list.append(current_path)
                    self._file_info[current_path] = SevenZipFileInfo(
                        name=current_path,
                        size=current_size,
                        compressed=current_compressed
                    )
                
                current_path = line[7:]  # Remove "Path = " prefix
                current_size = 0
                current_compressed = 0
                
            elif line.startswith('Size = '):
                try:
                    current_size = int(line[7:])
                except ValueError:
                    current_size = 0
                    
            elif line.startswith('Packed Size = '):
                try:
                    current_compressed = int(line[14:])
                except ValueError:
                    current_compressed = 0
        
        # Don't forget the last file
        if current_path is not None:
            self._file_list.append(current_path)
            self._file_info[current_path] = SevenZipFileInfo(
                name=current_path,
                size=current_size,
                compressed=current_compressed
            )
    
    def getnames(self) -> List[str]:
        """Get list of file names in the archive."""
        self._parse_listing()
        return self._file_list.copy()
    
    def getinfo(self, name: str) -> Optional[SevenZipFileInfo]:
        """Get info about a specific file in the archive."""
        self._parse_listing()
        return self._file_info.get(name)
    
    def extract_to_bytes(self, name: str) -> bytes:
        """
        Extract a single file from the archive and return its contents as bytes.
        
        This uses 7-Zip's stdout extraction: 7zz e archive.7z -so filename
        """
        # Use -so to write to stdout, -y to assume yes
        # Use '--' to prevent filenames starting with '-' from being
        # interpreted as switches (e.g., '-23+147.dsf' for southern coords)
        result = self._run_7zip(['e', self.archive_path, '-so', '--', name])
        
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='ignore')
            raise SevenZipError(f"Failed to extract '{name}': {stderr}")
        
        return result.stdout
    
    def extract(self, targets: Optional[List[str]] = None, factory=None):
        """
        py7zr-compatible extract method.
        
        If factory is provided and has a 'products' dict, extracted files 
        will be stored there as BytesIO objects.
        """
        if targets is None:
            targets = self.getnames()
        
        for target in targets:
            data = self.extract_to_bytes(target)
            
            if factory is not None and hasattr(factory, 'products'):
                # Create a BytesIO-like object with the data
                bio = io.BytesIO(data)
                
                # Add size() method for compatibility
                def size_method(bio=bio):
                    pos = bio.tell()
                    bio.seek(0, io.SEEK_END)
                    sz = bio.tell()
                    bio.seek(pos)
                    return sz
                
                bio.size = size_method
                bio.seek(0)
                factory.products[target] = bio


class BytesIOFactory:
    """
    py7zr.io.BytesIOFactory compatible class.
    
    Collects extracted files as BytesIO objects.
    """
    
    def __init__(self, limit: int = 128 * 1024 * 1024):
        self.limit = limit
        self.products: Dict[str, io.BytesIO] = {}
