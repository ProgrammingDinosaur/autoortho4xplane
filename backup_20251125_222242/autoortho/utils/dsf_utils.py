"""module to handle dsf files"""
import os
import json
import shutil
import subprocess
import uuid
from logging import getLogger
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from aoconfig import CFG
from utils.constants import system_type

from enum import Enum

log = getLogger(__name__)

class XP12DSFNotFound(Exception):
    """Exception raised when a DSF file is not found in the XP12 Global Scenery or Demo Areas"""
    pass

class SeasonsAddResult(Enum):
    ADDED = "added"
    XP_TILE_MISSING = "tile_missing"
    FAILED = "failed"


class DsfUtils:

    SAFE_RASTERS = [
        "spr",
        "win",
        "fal",
        "sum",
        "soundscape",
        "elevation",
    ]

    def __init__(self):
        self.dsf_tool_location = self.get_dsf_tool_location()
        self.xplane_path = CFG.paths.xplane_path
        self.ao_path = CFG.paths.scenery_path
        self.global_scenery_path = os.path.join(self.xplane_path, "Global Scenery", "X-Plane 12 Global Scenery", "Earth nav data")
        self.demo_scenery_path = os.path.join(self.xplane_path, "Global Scenery", "X-Plane 12 Demo Areas", "Earth nav data")
        self.dsf_dir = CFG.paths.dsf_dir
        self.seven_zip_dir = self.get_7zip_location()    # compressing dsf files

    def _run_silent_subprocess(self, command):
        """Run external tools without popping a console window on Windows.

        - Suppresses stdout entirely and captures stderr for logging
        - Uses CREATE_NO_WINDOW on Windows to avoid opening new terminal windows
        """
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if system_type == "windows" else 0
        startupinfo = None
        if system_type == "windows":
            # Hide window even if the child is a console subsystem exe
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = 0  # SW_HIDE

        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )

    def compress_dsf_file(self, dsf_to_compress_path, compressed_dsf_path) -> bool:
        command = [
            self.seven_zip_dir,
            "a",
            "-t7z",
            "-m0=lzma",
            compressed_dsf_path,
            dsf_to_compress_path,
        ]
        try:
            result = self._run_silent_subprocess(command)
            return result.returncode == 0
        except subprocess.CalledProcessError as e:
            log.error(f"Failed to compress {dsf_to_compress_path} to {compressed_dsf_path}: {e}")
            return False
        except OSError as e:
            log.error(f"Failed to execute 7-zip at {self.seven_zip_dir}: {e}")
            return False


    def get_scenery_dsf_backup_dir(self, scenery_name):
        return os.path.join(self.ao_path, "z_autoortho", "scenery", scenery_name, "dsf_backups")

    def get_dsf_tool_location(self):
        if system_type == "windows":
            lib_subfolder = "windows"
        elif system_type == "linux":
            lib_subfolder = "linux"
        elif system_type == "darwin":
            lib_subfolder = "macos"
        else:
            raise ValueError(f"Unsupported system type: {system_type}")
        base_dir = os.path.dirname(os.path.dirname(__file__))
        binary_name = "DSFTool.exe" if system_type == "windows" else "DSFTool"
        return os.path.join(base_dir, "lib", lib_subfolder, binary_name)

    def get_7zip_location(self):
        if system_type == "windows":
            lib_dir = "windows/7zip/7za.exe"
        elif system_type == "linux":
            lib_dir = "linux/7zip/7zz"
        elif system_type == "darwin":
            lib_dir = "macos/7zip/7zz"
        else:
            raise ValueError(f"Unsupported system type: {system_type}")
        base_dir = os.path.dirname(os.path.dirname(__file__))
        return os.path.join(base_dir, "lib", lib_dir)

    def get_dsf_folder_location(self, dsf_folder, dsf_filename):
        # remove .dsf from the end of the dsf name
        
        tentative_path = os.path.join(self.global_scenery_path, dsf_folder, dsf_filename)
        if os.path.exists(tentative_path):
            return tentative_path
        else:
            fallback_path = os.path.join(self.demo_scenery_path, dsf_folder, dsf_filename)
            if os.path.exists(fallback_path):
                return fallback_path
            else:
                raise XP12DSFNotFound(f"Global DSF file does not exist in {tentative_path} or {fallback_path}")
    
    def convert_dsf_to_txt(self, dsf_file_path, txt_file_path):
        command = [
            self.dsf_tool_location,
            "--dsf2text",
            dsf_file_path,
            txt_file_path,
        ]
        try:
            result = self._run_silent_subprocess(command)
            return result.returncode == 0
        except subprocess.CalledProcessError as e:
            log.error(
                "DSFTool dsf2text failed for %s: %s",
                dsf_file_path,
                e.stderr.decode(errors="ignore"),
            )
            return False
        except OSError as e:
            log.error("Failed to execute DSFTool at %s: %s", self.dsf_tool_location, e)
            return False

    def convert_txt_to_dsf(self, txt_file_path, dsf_file_path):
        command = [
            self.dsf_tool_location,
            "--text2dsf",
            txt_file_path,
            dsf_file_path,
        ]
        try:
            result = self._run_silent_subprocess(command)
            return result.returncode == 0
        except subprocess.CalledProcessError as e:
            log.error(
                "DSFTool text2dsf failed for %s: %s",
                txt_file_path,
                e.stderr.decode(errors="ignore"),
            )
            return False
        except OSError as e:
            log.error("Failed to execute DSFTool at %s: %s", self.dsf_tool_location, e)
            return False

    def add_season_to_dsf_txt(self, package_name, dsf_folder, dsf_filename, cache_dir, processed_dsf_seasons) -> SeasonsAddResult:

        # BASED on script by hotbso https://github.com/hotbso/o4xp_2_xp12/blob/main/o4xp_2_xp12.py. Credit to him.

        skip_main_dsf = False
        if dsf_folder in processed_dsf_seasons:
            if dsf_filename in processed_dsf_seasons[dsf_folder]:
                log.info(f"DSF {dsf_folder}/{dsf_filename} already processed")
                return SeasonsAddResult.ADDED


        try:
            global_dsf_file_path = self.get_dsf_folder_location(dsf_folder, dsf_filename)
        except XP12DSFNotFound:
            log.warning(f"Global DSF file could not be found in XP12 Global Scenery or Demo Areas, this tile will be skipped")
            return SeasonsAddResult.XP_TILE_MISSING # Mark as completed since it's not an error per se

        ao_mesh_dsf_txt_file_path = f"{os.path.join(cache_dir, f"ao_{dsf_filename}.txt")}"
        global_dsf_txt_file_path = f"{os.path.join(cache_dir, f"global_{dsf_filename}.txt")}"

        # get the name of the dsf to parse
        os.makedirs(cache_dir, exist_ok=True)
        dsf_to_parse_location = os.path.join(self.ao_path, "z_autoortho", "scenery", package_name, "Earth nav data", dsf_folder, dsf_filename)

        if self.convert_dsf_to_txt(dsf_to_parse_location, ao_mesh_dsf_txt_file_path):
            with open(ao_mesh_dsf_txt_file_path, "r") as file:
                for line in file:
                    if line.startswith("RASTER_"):
                        log.debug(f"Found RASTER line, skipping file")
                        skip_main_dsf = True
                        break
        else:
            log.error(f"Failed to convert {dsf_to_parse_location} to txt")
            shutil.rmtree(cache_dir)
            log.debug(f"Removed cache directory {cache_dir}")
            return SeasonsAddResult.FAILED


        if skip_main_dsf:
            log.info(f"Skipping {dsf_to_parse_location} because it is already processed")
            shutil.rmtree(cache_dir)
            log.debug(f"Removed cache directory {cache_dir}")
            return SeasonsAddResult.ADDED

        raster_refs = []
        on_raster_refs = False
        if self.convert_dsf_to_txt(global_dsf_file_path, global_dsf_txt_file_path):
            with open(global_dsf_txt_file_path, "r") as file:
                for line in file:
                    if line.startswith("RASTER_") and any(raster in line for raster in self.SAFE_RASTERS):
                        on_raster_refs = True
                        raster_refs.append(line)
                    elif on_raster_refs:
                        break # stop at the first line after the raster refs
                    else:
                        continue
        else:
            log.error(f"Failed to convert {global_dsf_file_path} to txt")
            shutil.rmtree(cache_dir)
            log.debug(f"Removed cache directory {cache_dir}")
            return SeasonsAddResult.FAILED

        if not raster_refs:
            log.error(f"Global DSF file {global_dsf_file_path} does not contain any raster refs")
            shutil.rmtree(cache_dir)
            log.debug(f"Removed cache directory {cache_dir}")
            return SeasonsAddResult.FAILED


        if not skip_main_dsf:
            with open(ao_mesh_dsf_txt_file_path, "a") as file:
                for ref in raster_refs:
                    file.write(ref)


        temp_mesh_dsf_file_path = f"{os.path.join(cache_dir, f"temp_mesh_{dsf_filename}")}" 
        compressed_temp_mesh_dsf_file_path = f"{os.path.join(cache_dir, f"temp_mesh_7z_{dsf_filename}")}"

        if not skip_main_dsf:
            if self.convert_txt_to_dsf(ao_mesh_dsf_txt_file_path, temp_mesh_dsf_file_path):
                log.debug(f"Built temp mesh DSF for {dsf_to_parse_location}")
            else:
                log.error(f"Failed to build temp mesh DSF for {dsf_to_parse_location}")
                shutil.rmtree(cache_dir)
                log.debug(f"Removed cache directory {cache_dir}")
                return SeasonsAddResult.FAILED

            if CFG.seasons.compress_dsf:
                if self.compress_dsf_file(temp_mesh_dsf_file_path, compressed_temp_mesh_dsf_file_path):
                    log.debug(f"Compressed temp mesh DSF for {dsf_to_parse_location}")
                else:
                    shutil.rmtree(cache_dir)
                    log.debug(f"Removed cache directory {cache_dir}")
                    return SeasonsAddResult.FAILED

            main_backup_dir = os.path.join(self.get_scenery_dsf_backup_dir(package_name), dsf_folder)
            os.makedirs(main_backup_dir, exist_ok=True)
            backup_path = os.path.join(main_backup_dir, dsf_filename + ".bak")
            if not os.path.exists(backup_path):
                shutil.copy(dsf_to_parse_location, backup_path)
                log.debug(f"Backed up old {dsf_to_parse_location}")
            else:
                log.debug(f"Backup exists for {backup_path}")

            baked_dsf_file_path = compressed_temp_mesh_dsf_file_path if CFG.seasons.compress_dsf else temp_mesh_dsf_file_path
            shutil.move(baked_dsf_file_path, dsf_to_parse_location)
            log.debug(f"Moved new {baked_dsf_file_path} to {dsf_to_parse_location}")

        shutil.rmtree(cache_dir)
        log.debug(f"Removed cache directory {cache_dir}")

        return SeasonsAddResult.ADDED

    def scan_for_dsfs(self, scenery_package_path):
        total_dsfs = 0
        dsf_folder_files = {}
        dsf_folders = os.path.join(self.ao_path, "z_autoortho", "scenery", scenery_package_path, "Earth nav data")
        log.debug(f"Scanning for dsfs in {dsf_folders}")
        if not os.path.isdir(dsf_folders):
            return {}, 0
        for folder in os.listdir(dsf_folders):
            if os.path.isdir(os.path.join(dsf_folders, folder)):
                for file in os.listdir(os.path.join(dsf_folders, folder)):
                    if file.endswith(".dsf"):
                        if folder not in dsf_folder_files:
                            dsf_folder_files[folder] = []
                        dsf_folder_files[folder].append(os.path.join(file))
                        total_dsfs += 1
        log.debug(f"Found {total_dsfs} dsfs in {scenery_package_path}")
        return dsf_folder_files, total_dsfs

    def add_seasons_to_package(self, scenery_name:str, progress_callback=None):
        # try to load a json file that contains the list of dsfs that have already been processed
        log.debug(f"Adding seasons to {scenery_name}")
        scenery_info_json = os.path.join(self.ao_path, "z_autoortho", scenery_name.replace("z_ao_", "") + "_info.json")
        if os.path.exists(scenery_info_json):
            with open(scenery_info_json, "r") as file:
                scenery_info = json.load(file)
                dsf_folder_files = scenery_info.get("dsf_folder_files", {})
                processed_dsf_seasons = scenery_info.get("processed_dsf_seasons", {})
                missing_xp_tiles = scenery_info.get("missing_xp_tiles", {})
                total_dsfs = scenery_info.get("total_dsfs", 0)
        else:
            scenery_info = {}
            dsf_folder_files = {}
            total_dsfs = 0
            processed_dsf_seasons = {}
            missing_xp_tiles = {}

        if not dsf_folder_files and not processed_dsf_seasons:
            dsf_folder_files, total_dsfs = self.scan_for_dsfs(scenery_name)
            log.debug(f"Found {total_dsfs} dsf seasons in {scenery_name}")

        

        files_done = 0
        failures = 0
        workers = int(CFG.seasons.seasons_convert_workers)

        if progress_callback:
            progress_callback({"pcnt_done": 0, "status": "Processing...", "files_done": files_done, "files_total": total_dsfs})

        # Stream tasks into the executor instead of queueing all at once
        def _iter_tasks():
            for dsf_folder, dsf_files in dsf_folder_files.items():
                for dsf_file in dsf_files:
                    yield dsf_folder, dsf_file

        with ThreadPoolExecutor(max_workers=workers) as executor:
            tasks = _iter_tasks()
            in_flight = {}

            # Prime up to max workers
            try:
                for _ in range(max(1, workers)):
                    dsf_folder, dsf_file = next(tasks)
                    fut = executor.submit(
                        self.add_season_to_dsf_txt,
                        scenery_name,
                        dsf_folder,
                        dsf_file,
                        os.path.join(self.dsf_dir, str(uuid.uuid4())),
                        processed_dsf_seasons,
                    )
                    in_flight[fut] = (dsf_folder, dsf_file)
            except StopIteration:
                pass

            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    dsf_folder, dsf_file = in_flight.pop(future)
                    try:
                        success = future.result()
                        if success == SeasonsAddResult.ADDED:
                            if dsf_folder not in processed_dsf_seasons:
                                processed_dsf_seasons[dsf_folder] = [dsf_file]
                            elif dsf_file not in processed_dsf_seasons[dsf_folder]:
                                processed_dsf_seasons[dsf_folder].append(dsf_file)
                        elif success == SeasonsAddResult.XP_TILE_MISSING:
                            if dsf_folder not in missing_xp_tiles:
                                missing_xp_tiles[dsf_folder] = [dsf_file]
                            elif dsf_file not in missing_xp_tiles[dsf_folder]:
                                missing_xp_tiles[dsf_folder].append(dsf_file)
                        else:
                            log.error(f"Failed to add season to {dsf_folder}/{dsf_file}")
                            failures += 1
                        files_done += 1
                    except Exception as e:
                        log.error(f"Error adding season to {dsf_folder}/{dsf_file}: {e}")
                        failures += 1
                        files_done += 1
                    finally:
                        if progress_callback:
                            pcnt = int((files_done / total_dsfs) * 100) if total_dsfs else 100
                            progress_callback({"pcnt_done": pcnt, "files_done": files_done, "files_total": total_dsfs, "failures": failures})

                    # Keep the pipeline full
                    try:
                        dsf_folder, dsf_file = next(tasks)
                        fut = executor.submit(
                            self.add_season_to_dsf_txt,
                            scenery_name,
                            dsf_folder,
                            dsf_file,
                            os.path.join(self.dsf_dir, str(uuid.uuid4())),
                            processed_dsf_seasons,
                        )
                        in_flight[fut] = (dsf_folder, dsf_file)
                    except StopIteration:
                        pass
        
        # only change the keys we need to change
        scenery_info.update({
            "dsf_folder_files": dsf_folder_files,
            "processed_dsf_seasons": processed_dsf_seasons,
            "total_dsfs": total_dsfs,
            "missing_xp_tiles": missing_xp_tiles
        })
        tmp_path = scenery_info_json + ".tmp"
        with open(tmp_path, "w") as file:
            json.dump(scenery_info, file, indent=4)
        os.replace(tmp_path, scenery_info_json)

        # If all dsfs have been processed or skipped due to missing XP tiles, return True
        # Compare in a set-wise, order-insensitive manner and de-duplicate any overlaps
        def _to_dict_of_sets(folder_to_files):
            normalized = {}
            for folder, files in folder_to_files.items():
                normalized[folder] = set(files)
            return normalized

        merged_dsf_folder_files_sets = _to_dict_of_sets(processed_dsf_seasons)
        for folder, files in missing_xp_tiles.items():
            if folder not in merged_dsf_folder_files_sets:
                merged_dsf_folder_files_sets[folder] = set(files)
            else:
                merged_dsf_folder_files_sets[folder].update(files)

        dsf_folder_files_sets = _to_dict_of_sets(dsf_folder_files)

        return dsf_folder_files_sets == merged_dsf_folder_files_sets


    def restore_default_dsfs(self, scenery_name:str, progress_callback=None):
        log.debug(f"Restoring default DSFs for {scenery_name}")
        scenery_info_json = os.path.join(self.ao_path, "z_autoortho", scenery_name.replace("z_ao_", "") + "_info.json")
        if os.path.exists(scenery_info_json):
            with open(scenery_info_json, "r") as file:
                scenery_info = json.load(file)
                processed_dsf_seasons = scenery_info.get("processed_dsf_seasons", {})
        else:
            log.error(f"Scenery info json does not exist for {scenery_name}")
            return False
        
        files_done = 0
        total_dsfs = scenery_info.get("total_dsfs", 0)
        if progress_callback:
            progress_callback({"pcnt_done": 0, "status": "Restoring default DSFs...", "files_done": files_done, "files_total": total_dsfs})

        for dsf_folder, dsf_files in processed_dsf_seasons.items():
            for dsf_file in dsf_files:
                folder_backup_path = os.path.join(self.get_scenery_dsf_backup_dir(scenery_name), dsf_folder)

                dsf_file_path = os.path.join(self.ao_path, "z_autoortho", "scenery", scenery_name, "Earth nav data", dsf_folder, dsf_file)
                backup_path = os.path.join(self.get_scenery_dsf_backup_dir(scenery_name), dsf_folder, dsf_file + ".bak")
                if os.path.exists(backup_path):
                    os.remove(dsf_file_path)
                    shutil.move(backup_path, dsf_file_path)
                    log.debug(f"Moved new {backup_path} to {dsf_file_path}")
                else:
                    log.debug(f"Backup does not exist for {dsf_file_path}")

                files_done += 1
                if progress_callback:
                    pcnt = int((files_done / total_dsfs) * 100) if total_dsfs else 100
                    progress_callback({"pcnt_done": pcnt, "files_done": files_done, "files_total": total_dsfs})
            shutil.rmtree(folder_backup_path)
            log.debug(f"Removed backup directory {folder_backup_path}")
        
        # reset fields in scenery info json
        scenery_info.update({
            "processed_dsf_seasons": {},
            "missing_xp_tiles": {},
        })
        tmp_path = scenery_info_json + ".tmp"
        with open(tmp_path, "w") as file:
            json.dump(scenery_info, file, indent=4)
        os.replace(tmp_path, scenery_info_json)
                    
        return True
     

dsf_utils = DsfUtils()
