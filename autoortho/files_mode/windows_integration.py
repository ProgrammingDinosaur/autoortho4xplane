"""Keep Windows Search and Microsoft Defender off the files folder.

Files mode creates and rewrites tens of thousands of files per flight.
OrthoStudio XP measured Windows Search indexing them at half a CPU core, and
Defender scans every new file. Both are opt-in:

* ``[files] exclude_from_search``: a Windows Search crawl-scope rule that
  excludes the files folder for this user (``ISearchCrawlScopeManager``,
  called through ctypes; no admin rights, no extra package).
* Defender: adding an exclusion needs admin rights, so it is only ever done
  on the user's request, through Windows' own elevation prompt
  (:func:`request_defender_exclusion`), or by the installer's unchecked
  option.
"""

import ctypes
import logging
import os
import sys
import threading
import uuid

log = logging.getLogger(__name__)

CLSID_CSEARCH_MANAGER = "{7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}"
IID_ISEARCH_MANAGER = "{AB310581-AC80-11D1-8DF3-00C04FB6EF69}"
CLSCTX_LOCAL_SERVER = 0x4
FF_INDEXCOMPLEXURLS = 0x1
# vtable slots (searchapi.h): IUnknown takes 0-2.
SLOT_RELEASE = 2
SLOT_GET_CATALOG = 10              # ISearchManager::GetCatalog
SLOT_GET_CRAWL_SCOPE_MANAGER = 28  # ISearchCatalogManager::GetCrawlScopeManager
SLOT_ADD_USER_SCOPE_RULE = 8       # ISearchCrawlScopeManager::AddUserScopeRule
SLOT_INCLUDED_IN_CRAWL_SCOPE = 13  # ISearchCrawlScopeManager::IncludedInCrawlScope
SLOT_SAVE_ALL = 16                 # ISearchCrawlScopeManager::SaveAll


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        u = uuid.UUID(text)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g


def _method(ptr, slot, *argtypes):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtable[slot])


def _release(ptr):
    if ptr:
        _method(ptr, SLOT_RELEASE)(ptr)


def _url(path):
    return "file:///" + os.path.abspath(path).rstrip("\\") + "\\"


class _CrawlScope:
    """The current user's crawl scope manager of the system index."""

    def __enter__(self):
        ole32 = ctypes.WinDLL("ole32")
        self._ole32 = ole32
        hr = ole32.CoInitializeEx(None, 0x2)  # apartment threaded
        self._uninit = hr in (0, 1)
        self.manager = ctypes.c_void_p()
        self.catalog = ctypes.c_void_p()
        self.scope = ctypes.c_void_p()
        clsid = _GUID.parse(CLSID_CSEARCH_MANAGER)
        iid = _GUID.parse(IID_ISEARCH_MANAGER)
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_LOCAL_SERVER,
                                    ctypes.byref(iid), ctypes.byref(self.manager))
        if hr != 0:
            raise OSError(f"Windows Search unavailable (0x{hr & 0xFFFFFFFF:08X})")
        hr = _method(self.manager, SLOT_GET_CATALOG, ctypes.c_wchar_p,
                     ctypes.POINTER(ctypes.c_void_p))(
            self.manager, "SystemIndex", ctypes.byref(self.catalog))
        if hr != 0:
            raise OSError(f"Search catalog unavailable (0x{hr & 0xFFFFFFFF:08X})")
        hr = _method(self.catalog, SLOT_GET_CRAWL_SCOPE_MANAGER,
                     ctypes.POINTER(ctypes.c_void_p))(self.catalog, ctypes.byref(self.scope))
        if hr != 0:
            raise OSError(f"Crawl scope unavailable (0x{hr & 0xFFFFFFFF:08X})")
        return self

    def included(self, path):
        flag = ctypes.c_int(0)
        hr = _method(self.scope, SLOT_INCLUDED_IN_CRAWL_SCOPE, ctypes.c_wchar_p,
                     ctypes.POINTER(ctypes.c_int))(self.scope, _url(path), ctypes.byref(flag))
        if hr < 0:
            raise OSError(f"IncludedInCrawlScope failed (0x{hr & 0xFFFFFFFF:08X})")
        return bool(flag.value)

    def exclude(self, path):
        hr = _method(self.scope, SLOT_ADD_USER_SCOPE_RULE, ctypes.c_wchar_p, ctypes.c_int,
                     ctypes.c_int, ctypes.c_uint32)(
            self.scope, _url(path), 0, 1, FF_INDEXCOMPLEXURLS)
        if hr < 0:
            raise OSError(f"AddUserScopeRule failed (0x{hr & 0xFFFFFFFF:08X})")
        hr = _method(self.scope, SLOT_SAVE_ALL)(self.scope)
        if hr < 0:
            raise OSError(f"SaveAll failed (0x{hr & 0xFFFFFFFF:08X})")

    def __exit__(self, *exc):
        for ptr in (self.scope, self.catalog, self.manager):
            try:
                _release(ptr)
            except Exception:
                pass
        if self._uninit:
            self._ole32.CoUninitialize()
        return False


def search_indexes(path):
    """True/False whether Windows Search indexes ``path``; None if unknown."""
    if sys.platform != "win32":
        return None
    try:
        with _CrawlScope() as scope:
            return scope.included(path)
    except Exception as exc:
        log.debug("Windows Search state unknown: %s", exc)
        return None


def exclude_from_search(path):
    """Exclude ``path`` from Windows Search for this user. Returns True when
    it is (now or already) excluded."""
    if sys.platform != "win32":
        return False
    try:
        with _CrawlScope() as scope:
            if not scope.included(path):
                return True
            scope.exclude(path)
            done = not scope.included(path)
        log.info("Windows Search: %s %s", path, "excluded" if done else "still indexed")
        return done
    except Exception as exc:
        log.warning("Could not exclude %s from Windows Search: %s", path, exc)
        return False


def exclude_from_search_async(path):
    """Run :func:`exclude_from_search` on its own thread (COM apartment)."""
    t = threading.Thread(target=exclude_from_search, args=(path,),
                         name="AO-SearchExclusion", daemon=True)
    t.start()
    return t


def defender_command(path):
    safe = os.path.abspath(path).replace("'", "''")
    return f"Add-MpPreference -ExclusionPath '{safe}'"


def request_defender_exclusion(path):
    """Ask Windows to add a Defender exclusion for ``path``.

    Windows shows its elevation prompt; nothing happens without the user's
    approval there. Returns True when the prompt was shown and accepted.
    """
    if sys.platform != "win32":
        return False
    shell32 = ctypes.WinDLL("shell32")
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    params = f'-NoProfile -NonInteractive -Command "{defender_command(path)}"'
    result = shell32.ShellExecuteW(None, "runas", "powershell.exe", params, None, 0)
    ok = (result or 0) > 32
    log.info("Defender exclusion for %s: %s", path, "requested" if ok else "declined")
    return ok
