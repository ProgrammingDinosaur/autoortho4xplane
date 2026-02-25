#!/usr/bin/env python3

import os
import re
import sys
import glob
import time
import json
import pprint
import shutil
import hashlib
import zipfile
import argparse
import platform
import threading
import subprocess
import requests
import requests.adapters
from datetime import datetime, timezone, timedelta
from packaging import version
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum


# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

import logging
log = logging.getLogger(__name__)

TESTMODE=os.environ.get('AO_TESTMODE', False)


_download_session = None
_session_lock = threading.Lock()

def _get_download_session(pool_size=10):
    """Return a module-level requests.Session with connection pooling."""
    global _download_session
    if _download_session is None:
        with _session_lock:
            if _download_session is None:
                session = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=pool_size,
                    pool_maxsize=pool_size,
                    max_retries=0,
                    pool_block=True,
                )
                session.mount('https://', adapter)
                session.mount('http://', adapter)
                _download_session = session
    return _download_session


def do_url(url, headers={}):
    session = _get_download_session()
    resp = session.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.content


cur_activity = {}

class SeasonsApplyStatus(Enum):
    NOT_APPLIED = "not_applied"
    PARTIALLY_APPLIED = "partially_applied"
    APPLIED = "applied"


class RoughnessApplyStatus(Enum):
    NOT_APPLIED = "not_applied"
    PARTIALLY_APPLIED = "partially_applied"
    APPLIED = "applied"


class Zip(object):
    # A zip object that may be made up of one or more files

    zf = None
    hashfile = '' 
    assembled = False
    computed_hash = None

    def __init__(self, path):
        self.path = path
        self.files = []

    def check(self, precomputed_hash=None):

        if not os.path.exists(self.path):
            log.info(f"File does not exist. {self.path}")
            return False

        if self.hashfile:
            log.info(f"Found hashfile {self.hashfile} verifying ...")
            with open(self.hashfile, "r") as h:
                data = h.read()
            m = re.match(r'(^[0-9A-Fa-f]+)\s+(\S.*)$', data)
            if not m:
                log.error(f"Could not parse hashfile {self.hashfile}")
                return False

            expected_hash = m.group(1)

            # Use pre-computed hash if available (from download or assembly)
            if precomputed_hash:
                if precomputed_hash == expected_hash:
                    log.info(f"Hash verified (pre-computed) for {self.path}")
                    return True
                log.error(f"Hash check failed for {self.path} : {expected_hash} != {precomputed_hash}")
                return False

            with open(self.path, "rb") as h:
                sha256 = hashlib.sha256()
                while True:
                    data = h.read(131072)
                    if not data:
                        break
                    sha256.update(data)
                ziphash = sha256.hexdigest()

            if expected_hash == ziphash:
                return True

            log.error(f"Hash check failed for {self.path} : {expected_hash} != {ziphash}")
            return False

        try:
            with zipfile.ZipFile(self.path) as zf:
                ret = zf.testzip()
                if ret:
                    log.error(f"Errors detected with zipfile {zf}\nFirst bad file: {ret}")
                    return False
        except Exception as err:
            log.error(f"Error with zip {err}")
            return False

        return True

    def __repr__(self):
        return(f"Zip({self.path})")

    def assemble(self):
        if any(x.endswith('.zip') for x in self.files) or self.assembled:
            log.info(f"No assembly required for {self.path}")
            return

        self.files.sort()
        log.info(f"Will assemble {self.path} from parts: {self.files}")
        sha256 = hashlib.sha256()
        with open(self.path, 'wb') as out_h:
            for f in self.files:
                if os.path.exists(f):
                    with open(f, 'rb') as in_h:
                        while True:
                            buf = in_h.read(1048576)
                            if not buf:
                                break
                            out_h.write(buf)
                            sha256.update(buf)
                    log.info(f"Removing {f}")
                    os.remove(f)

        self.computed_hash = sha256.hexdigest()
        self.assembled = True

    def extract(self, dest):
        with zipfile.ZipFile(self.path) as zf:
            zf.extractall(dest)

    # This approach is faster than extract, copytree, rmtree
    def extractScenery_y(self, destpath):
        with zipfile.ZipFile(self.path) as zf:
            destpath = os.path.join(destpath, "Earth nav data")
            os.makedirs(destpath, exist_ok=True)
            zip_path = zipfile.Path(zf)  # This is the path to and within the zip file

            children = []
            for entry in zip_path.iterdir():
                if (zip_path / entry.name).is_dir():
                    children.append(entry.name)
            if not len(children) == 1:
                log.error(
                    f"Unexpected directory structure in {zf}, too many top-level children."
                )
                return

            zip_path = zip_path / children[0] / "yOrtho4XP_Overlays" / "Earth nav data"
            if not zip_path.is_dir():
                log.error(f"'yOrtho4XP_Overlays/Earth nav data' not found in {zf}")
                return

            for dir in zip_path.iterdir():
                # Some zip files have a directory with this name.  Invalid and unexpected!
                if dir.name == "*":
                    continue
                os.makedirs(os.path.join(destpath, dir.name), exist_ok=True)
                for entry in (zip_path / dir.name).iterdir():
                    with (
                        entry.open(mode="rb") as source,
                        open(
                            os.path.join(destpath, dir.name, entry.name),
                            "wb",
                        ) as dest,
                    ):
                        shutil.copyfileobj(source, dest)

    # This approach is faster than extract, copytree, rmtree
    def extractScenery_z(self, destpath):
        with zipfile.ZipFile(self.path) as zf:
            os.makedirs(destpath, exist_ok=True)
            zip_path = zipfile.Path(zf)  # This is the path to and within the zip file

            children = []
            for entry in zip_path.iterdir():
                if (zip_path / entry.name).is_dir():
                    children.append(entry.name)
            if not len(children) == 1:
                log.error(
                    f"Unexpected directory structure in {zf}, too many top-level children."
                )
                return

            zip_path = zip_path / children[0]

            for dir in zip_path.iterdir():
                os.makedirs(os.path.join(destpath, dir.name), exist_ok=True)
                if dir.name == "textures" or dir.name == "terrain":
                    for entry in (zip_path / dir.name).iterdir():
                        with (
                            entry.open(mode="rb") as source,
                            open(
                                os.path.join(destpath, dir.name, entry.name), "wb"
                            ) as dest,
                        ):
                            shutil.copyfileobj(source, dest)
                elif dir.name == "Earth nav data":
                    earth_path = zip_path / dir.name
                    for earth_dir in earth_path.iterdir():
                        os.makedirs(
                            os.path.join(destpath, dir.name, earth_dir.name),
                            exist_ok=True,
                        )
                        for entry in (earth_path / earth_dir.name).iterdir():
                            with (
                                entry.open(mode="rb") as source,
                                open(
                                    os.path.join(
                                        destpath, dir.name, earth_dir.name, entry.name
                                    ),
                                    "wb",
                                ) as dest,
                            ):
                                shutil.copyfileobj(source, dest)

    def clean(self):
        if os.path.exists(self.path):
            log.info(f"Removing {self.path}")
            os.remove(self.path)

        for f in self.files:
            if os.path.exists(f):
                log.info(f"Removing {f}")
                os.remove(f)
            partpath = f + '.part'
            if os.path.exists(partpath):
                log.info(f"Removing {partpath}")
                os.remove(partpath)

        if os.path.exists(self.hashfile):
            log.info(f"Removing {self.hashfile}")
            os.remove(self.hashfile)

        self.files = []
        self.assembled = False
        self.computed_hash = None


class Package(object):
    # A package asset

    name = None
    download_dir = ""
    install_dir = ""
    size = 0
    regex_for_assembled_files =  r"^([\w]+\.zip)\.\d+$"
    max_retries = 5

    installed = False
    downloaded = False
    cleaned = False

    download_count = 0

    zf = None
    
    def __init__(
        self,
        name,
        pkgtype,
        download_dir = ".",
    ):
        self.name = name
        self.pkgtype = pkgtype
        self.download_dir = download_dir
        self.zf = Zip(os.path.join(
            self.download_dir, f"{self.name}.zip"
        ))
        self.remote_urls = []

    def __repr__(self):
        return(f"Package: {str(self.__dict__)}")
        #return(str(self.__dict__))


    def download(self, progress_callback=None, progress_state=None,
                 session=None, progress_lock=None):
        if self.downloaded:
            log.info(f"Already downloaded.")
            return

        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

        if session is None:
            session = _get_download_session()

        self.progress_callback = progress_callback
        self.progress_state = progress_state
        self._progress_lock = progress_lock
        self._last_progress_emit_ts = 0
        self._last_progress_emit_pcnt = -1

        for url in self.remote_urls:
            if progress_callback:
                init_progress = {
                    'status': f"Downloading and/or verifying {url}",
                    'pcnt_done': 0,
                    'MBps': 0,
                }
                if isinstance(self.progress_state, dict):
                    init_progress['files_done'] = self.progress_state.get('files_done', 0)
                    init_progress['files_total'] = self.progress_state.get('files_total', 0)
                progress_callback(init_progress)
            else:
                cur_activity['status'] = f"Downloading and/or verifying {url}"

            possible_destpath = ""
            filename = os.path.basename(url)
            destpath = os.path.join(self.download_dir, filename)
            partpath = destpath + '.part'
            log.info(f"Considering downloading {filename}")
            match = re.match(self.regex_for_assembled_files, filename)
            if match:
                possible_destpath = os.path.join(self.download_dir, match.group(1))
                log.info(f"{filename} is a possible part of the multi-part file {match.group(1)}")

            if os.path.isfile(destpath) or os.path.isfile(possible_destpath):
                # If the assembled multipart file already exists, add it only once
                if os.path.isfile(possible_destpath):
                    log.info(f"{possible_destpath} already exists.  Skipping {destpath}.")
                    if possible_destpath not in self.zf.files:
                        self.zf.files.append(possible_destpath)
                else:
                    log.info(f"{destpath} already exists.  Skip.")
                    if destpath.endswith('sha256'):
                        self.zf.hashfile = destpath
                    else:
                        self.zf.files.append(destpath)
                self._increment_files_done()
                continue

            # file:// URLs (used in tests) bypass the requests session
            if url.startswith('file://'):
                from urllib.parse import urlparse
                from urllib.request import url2pathname
                local_path = url2pathname(urlparse(url).path)
                shutil.copy2(local_path, destpath)
                log.info(f"Copied local file {local_path} -> {destpath}")
                self._increment_files_done()
                if destpath.endswith('sha256'):
                    self.zf.hashfile = destpath
                else:
                    self.zf.files.append(destpath)
                continue

            retries = 0
            while retries < self.max_retries:
                try:
                    log.info(f"Download {url}")
                    dl_start_time = time.time()

                    resume_pos = 0
                    req_headers = {}
                    if os.path.isfile(partpath):
                        resume_pos = os.path.getsize(partpath)
                        if resume_pos > 0:
                            req_headers['Range'] = f'bytes={resume_pos}-'
                            log.info(f"Resuming download from byte {resume_pos}")

                    resp = session.get(url, headers=req_headers, stream=True, timeout=30)
                    resp.raise_for_status()

                    content_length = int(resp.headers.get('content-length', 0))

                    if resume_pos > 0 and resp.status_code == 206:
                        total_size = resume_pos + content_length
                        file_mode = 'ab'
                        fetched = resume_pos
                    else:
                        total_size = content_length
                        file_mode = 'wb'
                        fetched = 0
                        resume_pos = 0

                    sha256 = hashlib.sha256()

                    # When resuming, hash the existing partial content first
                    if file_mode == 'ab' and resume_pos > 0:
                        with open(partpath, 'rb') as h:
                            while True:
                                chunk = h.read(131072)
                                if not chunk:
                                    break
                                sha256.update(chunk)

                    with open(partpath, file_mode) as f:
                        for chunk in resp.iter_content(chunk_size=1048576):
                            f.write(chunk)
                            sha256.update(chunk)
                            fetched += len(chunk)
                            self._emit_progress(url, fetched, total_size, dl_start_time)

                    os.replace(partpath, destpath)

                    # For single-file zips, store hash for later verification
                    if destpath.endswith('.zip'):
                        self.zf.computed_hash = sha256.hexdigest()

                    log.info(f"DONE downloading {url}")
                    self._increment_files_done()
                    break

                except requests.RequestException as e:
                    retries += 1
                    log.error(f"Failed to download {url}, attempt {retries} of {self.max_retries}. Error: {e}")
                    time.sleep(2**retries)

            if retries == self.max_retries:
                log.error(f"Max retries reached. Failed to download {url}")

            if destpath.endswith('sha256'):
                self.zf.hashfile = destpath
            else:
                self.zf.files.append(destpath)

        self.downloaded = True
        self.zf.assemble()

    def _increment_files_done(self):
        """Thread-safe increment of files_done and emit completion progress."""
        if not isinstance(self.progress_state, dict):
            return

        if self._progress_lock:
            with self._progress_lock:
                self.progress_state['files_done'] = self.progress_state.get('files_done', 0) + 1
        else:
            self.progress_state['files_done'] = self.progress_state.get('files_done', 0) + 1

        if self.progress_callback:
            files_total = self.progress_state.get('files_total', 0)
            files_done = self.progress_state.get('files_done', 0)
            overall_pcnt = round((files_done / files_total) * 100, 2) if files_total else 0
            self.progress_callback({
                'status': f"Downloaded {files_done}/{files_total}",
                'pcnt_done': 100,
                'MBps': 0,
                'overall_pcnt': overall_pcnt,
                'files_done': files_done,
                'files_total': files_total,
            })

    def _emit_progress(self, url, fetched, total_size, start_time):
        """Emit download progress with throttling."""
        if total_size <= 0:
            return

        pcnt_done = round(fetched / total_size * 100, 2)
        elapsed = time.time() - start_time
        if not elapsed:
            return
        MBps = (fetched / 1048576) / elapsed

        progress_data = {
            'pcnt_done': pcnt_done,
            'MBps': MBps,
            'status': f"Downloading {url}\n{pcnt_done:.2f}%   {MBps:.2f} MBps",
        }

        if isinstance(self.progress_state, dict) and total_size > 0:
            files_total = self.progress_state.get('files_total', 0)
            files_done = self.progress_state.get('files_done', 0)
            if files_total:
                overall = ((files_done + (fetched / total_size)) / files_total) * 100
                progress_data['overall_pcnt'] = round(overall, 2)
                progress_data['files_done'] = files_done
                progress_data['files_total'] = files_total

        should_emit = False
        now = time.time()
        if self._last_progress_emit_ts == 0:
            should_emit = True
        elif abs(pcnt_done - self._last_progress_emit_pcnt) >= 1:
            should_emit = True
        elif now - self._last_progress_emit_ts >= 0.25:
            should_emit = True

        if should_emit:
            self._last_progress_emit_ts = now
            self._last_progress_emit_pcnt = pcnt_done
            if hasattr(self, 'progress_callback') and self.progress_callback:
                self.progress_callback(progress_data)
            else:
                cur_activity.update(progress_data)

        if not getattr(self, 'progress_callback', None):
            if int(fetched / 1048576) % 10 == 0:
                print(f"\r{pcnt_done:.2f}%   {MBps:.2f} MBps", end='')

    def check(self):
        log.info(f"Checking {self.name}")
        precomputed = getattr(self.zf, 'computed_hash', None)
        try:
            result = self.zf.check(precomputed_hash=precomputed) if precomputed else self.zf.check()
        except TypeError:
            result = self.zf.check()
        if not result:
            log.warning(f"{self.name} is bad.  Cleaning up.")
            self.cleanup()
            self.downloaded = False
            return False
        log.info(f"{self.name} is good.")
        return True

    def install(self):
        if self.installed:
            return

        if self.pkgtype == 'y':
            self.zf.extractScenery_y(self.install_dir)
        if self.pkgtype == 'z':
            self.zf.extractScenery_z(self.install_dir)

        self.installed = True

    def uninstall(self):
        if os.path.exists(self.install_dir):
            log.info(f"Dir exists.  Uninstalling {self.install_dir}")
            shutil.rmtree(self.install_dir)
        self.installed = False

    def cleanup(self):
        self.zf.clean()
        self.cleaned = True
        self.downloaded = False


class Release(object):

    # A release of orthos

    download_dir = ""
    install_dir = ""
    totalsize = 0
    ver = "0.0.0"
    url = ""
    seasons_apply_status = SeasonsApplyStatus.NOT_APPLIED
    roughness_apply_status = RoughnessApplyStatus.NOT_APPLIED
    install_name = ""

    installed = False
    downloaded = False
    cleaned = False
    parsed = False
    legacy = False

    download_count = 0
    info_ver = "v2"


    def __init__(
        self,
        name,
        install_dir = "Custom Scenery",
        release_dict = None,
        url = "",
        download_dir = "downloads",
    ):
        self.name = name
        self.install_dir = install_dir
        self.download_dir = download_dir
        self.url = url
        self.release_dict = release_dict if release_dict else {}
        self.packages = {}
        self.info_path = os.path.join(self.install_dir, "z_autoortho", f"{self.name}_info.json")
        self.subfolder_dir = os.path.join(self.install_dir, "z_autoortho", "scenery", f"z_ao_{self.name}")
        self.ortho_dirs = []
        self.info_ver = "v2"
        self.dsf_folders_files = {}
        self.processed_dsf_seasons = {}
        self.total_dsfs = 0
        self.missing_xp_tiles = {}
        # SUPER_ROUGHNESS patching tracking
        self.ter_files = {}
        self.processed_ter_roughness = {}
        self.total_ters = 0
        self.roughness_value = None

        #if os.path.exists(self.info_path):
        #    self.load(self.info_path)


    def __repr__(self):
        return(f"Release({self.ver}, {self.info_ver}, {self.url})")
        #return(f"Release({self.ver}, {self.install_dir}, {self.download_dir}, {self.url})")


    def load(self, info_path):
        log.info(f"Loading local info from {info_path}")
        with open(info_path) as h:
            info = json.loads(h.read())

        # Set attrs from info json
        for k,v in info.items():
            setattr(self, k, v)

        self.installed = True
        self.downloaded = True
        self.cleaned = True

        # Determine seasons status by comparing (processed ∪ missing) to
        # the expected DSF list, using order-insensitive set logic
        def _to_dict_of_sets(folder_to_files):
            normalized = {}
            for folder, files in (folder_to_files or {}).items():
                normalized[folder] = set(files)
            return normalized

        expected = getattr(self, 'dsf_folder_files', None)
        if expected is None:
            expected = getattr(self, 'dsf_folders_files', {})

        expected_sets = _to_dict_of_sets(expected)

        merged_sets = _to_dict_of_sets(self.processed_dsf_seasons)
        for folder, files in (self.missing_xp_tiles or {}).items():
            if folder not in merged_sets:
                merged_sets[folder] = set(files)
            else:
                merged_sets[folder].update(files)

        if (not expected_sets and not self.processed_dsf_seasons
                and not self.missing_xp_tiles):
            self.seasons_apply_status = SeasonsApplyStatus.NOT_APPLIED
        elif merged_sets == expected_sets:
            self.seasons_apply_status = SeasonsApplyStatus.APPLIED
        else:
            self.seasons_apply_status = SeasonsApplyStatus.PARTIALLY_APPLIED

        # Determine SUPER_ROUGHNESS status
        ter_files = getattr(self, 'ter_files', {})
        processed_ter_roughness = getattr(self, 'processed_ter_roughness', {})
        total_ters = getattr(self, 'total_ters', 0)
        self.roughness_value = getattr(self, 'roughness_value', None)

        # Count total processed ter files
        total_processed_ters = 0
        for subfolder, files_dict in processed_ter_roughness.items():
            if isinstance(files_dict, dict):
                total_processed_ters += len(files_dict)
            else:
                total_processed_ters += len(files_dict) if files_dict else 0

        # Count total expected ter files
        total_expected_ters = 0
        for subfolder, files in ter_files.items():
            total_expected_ters += len(files) if files else 0

        if not ter_files and not processed_ter_roughness:
            self.roughness_apply_status = RoughnessApplyStatus.NOT_APPLIED
        elif total_processed_ters >= total_expected_ters and total_expected_ters > 0:
            self.roughness_apply_status = RoughnessApplyStatus.APPLIED
        elif total_processed_ters > 0:
            self.roughness_apply_status = RoughnessApplyStatus.PARTIALLY_APPLIED
        else:
            self.roughness_apply_status = RoughnessApplyStatus.NOT_APPLIED

        detected_info_ver = info.get('info_ver')
        if self.ortho_dirs and detected_info_ver is None:
            # We have ortho_dirs but no detected info_ver.  This is v1
            log.info(f"Legacy release detected for {self.name}")
            self.info_ver = "v1"
            self.ver = "0.0.0"
            #self.downloaded = False
            #self.installed = False
            #self.cleaned = False
            self.legacy = True
        elif detected_info_ver:
            self.info_ver = detected_info_ver

    def save(self):
        log.info(f"Saving info to {self.info_path}")
        log.debug(self.__dict__.keys())
        info_dict = {k:v for k,v in self.__dict__.items() if k not in [
            'release_dict',
            'packages'
        ]}

        pprint.pprint(info_dict)
        with open(self.info_path, "w") as h:
            h.write(json.dumps(info_dict, indent=4, default=vars))

    def parse(self):
        log.info(f"Begin parsing info_dict for {self.name}")

        if self.parsed:
            return

        info = {}
        packages = []
        download_count = []

        #self.version = self.release_dict.get('tag_name')
        self.prerelease = self.release_dict.get('prerelease')
        
        #if self.info_ver == 'v1':
        #    log.info(f"Legacy release detected for {self.name}")

        # Find info json
        for a in self.release_dict.get('assets', []):
            asset_name = a.get('name')
            if asset_name.endswith("_info.json"):
                resp = do_url(a.get('browser_download_url'))
                info = json.loads(resp)
                #log.info(info)

        # Set attrs from info json
        for k,v in info.items():
            setattr(self, k, v)
       

        # Find assets
        for a in self.release_dict.get('assets', []):
            asset_name = a.get('name')
            self.totalsize += int(a.get('size'))

            m = re.match(
                r"(?P<pkgtype>[yz])_(?P<pkgname>.*)\.zip\.?(?P<pkgsub>\d*)",
                asset_name
            )
            if not m:
                log.debug(f"Unknown file {asset_name}")
                continue

            asset_info = m.groupdict()
           
            pkgtype = asset_info.get('pkgtype')    
            pkgsub = asset_info.get('pkgsub')
            pkgname = asset_info.get('pkgname')
            
            p = self.packages.setdefault(
                f"{pkgtype}_{pkgname}",
                Package(
                    f"{pkgtype}_{pkgname}",
                    pkgtype,
                    download_dir=self.download_dir
                )
            )
            
            if pkgtype == "y":
                # Overlay package
                #p.install_dir = f"{self.install_dir}/y_ao_{self.id}"
                p.install_dir = f"{self.install_dir}/yAutoOrtho_Overlays"
            elif pkgtype == "z":
                # Ortho package
                p.install_dir = f"{self.install_dir}/z_autoortho/scenery/z_ao_{self.id}"


            p.remote_urls.append(a.get('browser_download_url'))
            download_count.append(a.get('download_count'))

        if download_count:
            self.download_count = max(download_count)
        self.parsed = True

    
    def download(self, progress_callback=None):
        if self.downloaded:
            log.info(f"Already downloaded {self.name}")
            return True
    
        self.cleaned = False
        self.parse()

        total_files = 0
        for pkg in self.packages.values():
            total_files += len(pkg.remote_urls)
        progress_state = {'files_total': total_files, 'files_done': 0}
        progress_lock = threading.Lock()

        session = _get_download_session()

        try:
            max_workers = int(CFG.scenery.max_download_workers)
        except (AttributeError, ValueError):
            max_workers = 4
        max_workers = max(1, min(max_workers, len(self.packages)))

        def _download_package(pkg_name, pkg):
            log.info(f"Downloading {pkg_name}")
            pkg.download(
                progress_callback=progress_callback,
                progress_state=progress_state,
                session=session,
                progress_lock=progress_lock,
            )
            if not pkg.check():
                log.warning(f"{pkg_name} failed checks.  Retrying ...")
                pkg.cleanup()
                pkg.download(
                    progress_callback=progress_callback,
                    progress_state=progress_state,
                    session=session,
                    progress_lock=progress_lock,
                )
                if not pkg.check():
                    log.error(f"{pkg_name} failed again.  Exiting!")
                    return False
            return True

        success = True
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_download_package, k, v): k
                for k, v in self.packages.items()
            }
            for future in as_completed(futures):
                pkg_name = futures[future]
                try:
                    if not future.result():
                        success = False
                except Exception as err:
                    log.error(f"Download error for {pkg_name}: {err}")
                    success = False

        if not success:
            return False

        self.downloaded = True
        return True

    def install(self, progress_callback=None, noclean=False):
        if self.installed:
            log.info(f"Already installed {self.name}")
            return True
        
        log.info("Check for and cleanup existing installs..")
        for k,v in self.packages.items():
            if v.pkgtype == "z":
                log.debug(f"Cleaning up ortho type package: {v.name}.")
                v.uninstall()

        self.parse()
        # Verify and install all packages in parallel
        if not self.verify_and_install_all(progress_callback):
            log.error(f"Verification/Install failed for {self.name}")
            self.downloaded = False
            return False
        self.save()
        self.installed = True
        if not noclean:
            self.cleanup()
        return True

    def verify_and_install_all(self, progress_callback=None):
        """Verify and install all packages in parallel.

        Emits 'verify' stage progress where each completed unit is a
        package that has been verified and installed.
        """
        packages_to_process = []
        for pkg in self.packages.values():
            if pkg.zf and (pkg.zf.hashfile or os.path.exists(pkg.zf.path)):
                packages_to_process.append(pkg)
            else:
                # If no archive present, treat as failure
                log.error(f"Missing archive for {pkg.name}")

        total = len(packages_to_process)
        if total == 0:
            return True

        done = 0
        if progress_callback:
            progress_callback({
                'stage': 'verify',
                'verify_done': done,
                'verify_total': total,
                'verify_pcnt': 0,
                'status': 'Starting verification and installation',
            })

        success = True
        max_workers = min(4, total)

        def verify_then_install(pkg: Package) -> bool:
            if not pkg.check():
                return False
            try:
                pkg.install()
                return True
            except Exception as err:
                log.error(f"Install error for {pkg.name}: {err}")
                pkg.cleanup()
                return False

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_pkg = {executor.submit(verify_then_install, pkg): pkg for pkg in packages_to_process}
            for future in as_completed(future_to_pkg):
                ok = False
                try:
                    ok = future.result()
                except Exception as err:
                    log.error(f"Verify/Install error: {err}")
                    ok = False
                if not ok:
                    success = False

                done += 1
                if progress_callback:
                    pcnt = int((done / total) * 100)
                    progress_callback({
                        'stage': 'verify',
                        'verify_done': done,
                        'verify_total': total,
                        'verify_pcnt': pcnt,
                        'status': f'Verified & Installed {done}/{total}',
                    })

        return success

    def download_and_install(self, progress_callback=None, noclean=False):
        """Download and install packages in a pipelined fashion.

        Packages are submitted for extraction as soon as their download
        and hash verification complete, overlapping network I/O with
        decompression and disk writes.
        """
        if self.installed:
            log.info(f"Already installed {self.name}")
            return True

        self.cleaned = False
        self.parse()

        if not self.packages:
            self.downloaded = True
            self.installed = True
            self.save()
            return True

        # Remove previous z-type install dirs before any extraction begins
        for k, v in self.packages.items():
            if v.pkgtype == "z":
                log.debug(f"Cleaning up ortho type package: {v.name}.")
                v.uninstall()

        total_files = 0
        for pkg in self.packages.values():
            total_files += len(pkg.remote_urls)
        progress_state = {'files_total': total_files, 'files_done': 0}
        progress_lock = threading.Lock()

        session = _get_download_session()

        try:
            max_workers = int(CFG.scenery.max_download_workers)
        except (AttributeError, ValueError):
            max_workers = 4
        max_workers = max(1, min(max_workers, len(self.packages)))
        extract_workers = max(1, max_workers // 2)

        extract_total = len(self.packages)
        extract_done = [0]
        extract_lock = threading.Lock()
        extract_futures = []

        def _install_package(pkg):
            try:
                pkg.install()
            except Exception as err:
                log.error(f"Install error for {pkg.name}: {err}")
                pkg.cleanup()
                raise
            with extract_lock:
                extract_done[0] += 1

        def _download_then_submit(pkg_name, pkg, extract_exec):
            log.info(f"Downloading {pkg_name}")
            pkg.download(
                progress_callback=progress_callback,
                progress_state=progress_state,
                session=session,
                progress_lock=progress_lock,
            )
            if not pkg.check():
                log.warning(f"{pkg_name} failed checks. Retrying ...")
                pkg.cleanup()
                pkg.download(
                    progress_callback=progress_callback,
                    progress_state=progress_state,
                    session=session,
                    progress_lock=progress_lock,
                )
                if not pkg.check():
                    log.error(f"{pkg_name} failed again. Exiting!")
                    return False

            future = extract_exec.submit(_install_package, pkg)
            with extract_lock:
                extract_futures.append(future)
            return True

        success = True
        extract_executor = ThreadPoolExecutor(max_workers=extract_workers)

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as dl_executor:
                dl_futures = {
                    dl_executor.submit(
                        _download_then_submit, k, v, extract_executor
                    ): k
                    for k, v in self.packages.items()
                }
                for future in as_completed(dl_futures):
                    pkg_name = dl_futures[future]
                    try:
                        if not future.result():
                            success = False
                    except Exception as err:
                        log.error(f"Download error for {pkg_name}: {err}")
                        success = False

            # All downloads finished; wait for any extractions still running.
            if success:
                has_pending = any(not f.done() for f in extract_futures)

                for future in as_completed(list(extract_futures)):
                    try:
                        future.result()
                    except Exception as err:
                        log.error(f"Extract error: {err}")
                        success = False

                    if has_pending and progress_callback:
                        pcnt = int((extract_done[0] / extract_total) * 100)
                        progress_callback({
                            'stage': 'verify',
                            'verify_done': extract_done[0],
                            'verify_total': extract_total,
                            'verify_pcnt': pcnt,
                            'status': f'Installed {extract_done[0]}/{extract_total}',
                        })
        finally:
            extract_executor.shutdown(wait=True)

        if not success:
            self.downloaded = False
            return False

        self.downloaded = True
        self.installed = True
        self.save()
        if not noclean:
            self.cleanup()
        return True

    def cleanup(self):
        if self.cleaned:
            return

        for k,v in self.packages.items():
            print(f"Cleaning {k}")
            v.cleanup()
        self.cleaned = True

    def uninstall(self) -> bool:
        # self.cleanup()
        try:

            if os.path.exists(self.subfolder_dir):
                log.info(f"Removing {self.subfolder_dir}")
                shutil.rmtree(self.subfolder_dir)

            if os.path.exists(self.info_path):
                log.info(f"Removing {self.info_path}")
                os.remove(self.info_path)

            log.info(f"Release {self.name} uninstalled.")
            self.installed = False
        except Exception as err:
            log.error(f"Failed to uninstall {self.name}: {err}")
            return False
        return True
        #self.downloaded = False


class Region(object):

    # A Particular region of orthos and/or overlays

    #local_version = '0.0.0'
    local_rel = None

    def __init__(self, region_id, install_dir=".", download_dir="."):
        self.region_id = region_id
        self.releases = {}
        self.install_dir = install_dir
        self.download_dir = download_dir
        self.find_existing()


    def __repr__(self):
        return(f"Region({self.region_id})")

    def find_existing(self):
        log.info(f"Checking installed releases for {self.region_id}")
        local_rel_info = glob.glob(os.path.join(
            self.install_dir, "z_autoortho", f"{self.region_id}_info.json"
        ))
        
        for rel in [ os.path.basename(rel) for rel in local_rel_info ]:
            rel_name = re.match('(.*)_info.json', rel).groups()[0]
            log.info(f"Found local info file for {rel_name}")

            release = Release(
                name = rel_name,
                install_dir = self.install_dir,
                download_dir = self.download_dir,
            )
            # Load local info from _info.json
            release.load(release.info_path)

            if release.legacy:
                log.info(f"{rel} is a legacy release.")
                
            if release.ver not in self.releases:
                self.releases[release.ver] = release

            self.local_rel = release
            #self.local_version = release.ver
            #self.regions[release.id] = region


    def get_latest_release(self):
        releases = sorted(
            self.releases.values(), 
            key=lambda x: version.parse(x.ver),
            reverse=True
        )
        log.debug(f"SORTED: {releases}")
        return releases[0]

    def install_release(self, ver=None, progress_callback=None, noclean=False):

        if ver is None:
            rel = self.get_latest_release()
        else:
            rel = self.releases[ver]

        if self.local_rel and self.local_rel.ver == rel.ver:
            log.info(f"Requested version: {rel.ver} is already installed.")
            return
        elif self.local_rel:
            log.info("Local release detected.  Uninstalling...")
            # self.local_rel.uninstall()
            self.releases.pop(self.local_rel.ver)
            self.local_rel = None

        if not rel.download_and_install(
            progress_callback=progress_callback, noclean=noclean
        ):
            log.error(f"Failed to download and install release {rel}")
            return False

        return True


class OrthoManager(object):
    url = "https://api.github.com/repos/kubilus1/autoortho-scenery/releases"
    info_cache = os.path.join(os.path.expanduser("~"), ".autoortho-data", ".release_info")

    def __init__(self, extract_dir=None, download_dir=None, noclean=False):
        if not download_dir:
            download_dir = CFG.paths.download_dir
            #download_dir = os.path.join(os.path.expanduser("~"), ".autoortho-data", "downloads")
        if not extract_dir:
            extract_dir = CFG.paths.scenery_path

        self.download_dir = download_dir
        self.extract_dir = extract_dir
        self.noclean = noclean
        self.regions = {}
        
        overlay_list = []

        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)


    def _get_release_data(self):
        if os.path.exists(self.info_cache):
            mtime = os.path.getmtime(self.info_cache)
            last_updated_date = datetime.fromtimestamp(mtime)
            log.info(f"Last release refresh time: {last_updated_date}")
        else:
            last_updated_date = datetime.fromtimestamp(0)

        data = []
        if last_updated_date < (datetime.today() - timedelta(hours=1)):
            log.info(f"Check for updates ...")
            
            try:
                resp = do_url(
                    self.url,
                    headers = {"Accept": "application/vnd.github+json"}
                )
                with open(self.info_cache, "wb") as h:
                    h.write(resp)
                data = json.loads(resp)
            except Exception as err:
                log.warning(f"Couldn't update release info {err}.")

        if not data and os.path.exists(self.info_cache):
            log.info(f"Using cache ...")
            log.info(self.info_cache)
            with open(self.info_cache, "rb") as h:
                resp = h.read()
            data = json.loads(resp)
        
        return data

    def find_regions(self):
        self.regions = {}
        log.info(f"Looking for available regions ...")
        
        rel_data = self._get_release_data()

        log.info(f"Using scenery dir {self.extract_dir}")
        for item in rel_data:
            rel_ver = item.get('name')
            rel_id = item.get('id')

            #log.debug(f"VER: {rel_ver}, ID: {rel_id}")
            found_regions = [ 
                re.match('(.*)_info.json', x.get('name')).groups()[0] for x in item.get('assets') if x.get('name','').endswith('_info.json') 
            ]
            if not found_regions:
                continue

            region_name = found_regions[0]

            prerelease = item.get('prerelease')
            if prerelease and not TESTMODE:
                log.debug(f"{region_name}:{rel_ver} is pre-release. skipping.")
                continue

            if region_name not in self.regions:
                region = Region(
                    region_name,
                    install_dir = self.extract_dir,
                    download_dir = self.download_dir
                )
                self.regions[region_name] = region
            else:
                region = self.regions.get(region_name)

            if rel_ver not in region.releases:
                log.debug(f"Adding new release {rel_ver} to region {region_name}")
                release = Release(
                    name = region_name,
                    install_dir = self.extract_dir,
                    download_dir = self.download_dir,
                    url = f"{self.url}/{rel_id}",
                    release_dict = item
                )
                release.ver = rel_ver
                #release.parse()
                region.releases[rel_ver] = release
                #print(region.releases)
            #log.debug(region.releases)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    parser = argparse.ArgumentParser(
        description = "AutoOrtho Scenery Downloader"
    )
    subparser = parser.add_subparsers(help="command", dest="command")

    parser.add_argument(
        "--scenerydir",
        "-c",
        #default = "Custom Scenery",
        default = CFG.paths.scenery_path,
        help = "Path to X-Plane 'Custom Scenery' directory or other install path"
    )
    parser.add_argument(
        "--downloaddir",
        "-d",
        default = CFG.paths.download_dir,
        help = "Scenery temp download dir"
    )
    parser.add_argument(
        "--noclean",
        "-n",
        action = "store_true",
        help = "Disable cleaning of files after extraction."
    )
    parser.add_argument(
        "--downloadonly",
        "-o",
        action = "store_true",
        help = "Download only, don't extract files."
    )

    parser_list = subparser.add_parser('list')
    parser_fetch = subparser.add_parser('fetch')

    parser_fetch.add_argument(
        "region",
        nargs = "?",
        #choices = REGION_LIST, 
        help = "Which region to download and setup."
    )

    args = parser.parse_args()

    d = OrthoManager(
            os.path.expanduser(args.scenerydir),
            os.path.expanduser(args.downloaddir),
            noclean=args.noclean
    )

    if args.command == 'fetch':
        d.find_regions()
        region = args.region
        r = d.regions.get(region)
        rel = r.get_latest_release()
        rel.download()
        #d.download_region(region)
        if not args.downloadonly:
            #d.install(region)
            r.install_release()

    elif args.command == 'list':
        d.find_regions()
        for r in d.regions.values():
            rel = r.get_latest_release()
            rel.parse()
            localinfo = ""
            if r.local_rel:
                localinfo = f" ... installed {r.local_rel.name} version: {r.local_rel.ver}"
            log.info(f"{r.region_id} latest version: {rel.name} {rel.ver} {localinfo}")
            #print(f"{r} current version {r.local_version}")
            #log.info(f"{r} current version {r.local_version}")
            #if r.pending_update:
            #    log.info(f"    Available update ver: {r.latest_version}, size: {r.size/1048576:.2f} MB, downloads: {r.download_count}")
    else:
        parser.print_help()
        sys.exit(1)

    sys.exit(0)

