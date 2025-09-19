"""module to handle dsf files"""
import os
import json
import shutil
import subprocess
import uuid
from logging import getLogger
from concurrent.futures import ThreadPoolExecutor, as_completed

from aoconfig import CFG
from utils.constants import system_type

logger = getLogger(__name__)


class DsfUtils:

    GRID = 10
    SEASONS_LINES = [

    ]

    def __init__(self):
        self.dsf_tool_location = self.get_dsf_tool_location()
        self.xplane_path = CFG.paths.xplane_path
        self.ao_path = CFG.paths.scenery_path
        self.global_scenery_path = os.path.join(self.xplane_path, "Global Scenery", "X-Plane 12 Global Scenery", "Earth Nav Data")
        self.overlay_scenery_path = os.path.join(self.ao_path, "yAutoOrtho_Overlays", "Earth Nav Data")
        self.dsf_dir = CFG.paths.dsf_dir

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

    def get_dsf_folder_location(self, dsf_name, is_overlay=False):
        # remove .dsf from the end of the dsf name
        dsf_name = dsf_name.rstrip(".dsf")
        lat = str(dsf_name[:3])
        lon = str(dsf_name[3:])
        positive_lat = lat.startswith("+")
        positive_lon = lon.startswith("+")
        lat = lat.replace("+", "")
        lon = lon.replace("+", "")
        lat = float(lat)
        lon = float(lon)

        folder_lat = int(lat // self.GRID) * self.GRID
        folder_lon = int(lon // self.GRID) * self.GRID

        if positive_lat:
            folder_lat = "+" + str(folder_lat)
        if positive_lon:
            folder_lon = "+" + str(folder_lon)

        if len(folder_lon) == 3:
            folder_lon = folder_lon[:1] + "0" + folder_lon[1:]
        
        if is_overlay:
            return os.path.join(self.overlay_scenery_path, f"{folder_lat}{folder_lon}")
        else:
            return os.path.join(self.global_scenery_path, f"{folder_lat}{folder_lon}")
    
    def convert_dsf_to_txt(self, dsf_file_path, txt_file_path):
        command = [
            self.dsf_tool_location,
            "--dsf2text",
            dsf_file_path,
            txt_file_path,
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return result.returncode == 0
        except subprocess.CalledProcessError as e:
            logger.error(
                "DSFTool dsf2text failed for %s: %s",
                dsf_file_path,
                e.stderr.decode(errors="ignore"),
            )
            return False
        except OSError as e:
            logger.error("Failed to execute DSFTool at %s: %s", self.dsf_tool_location, e)
            return False

    def convert_txt_to_dsf(self, txt_file_path, dsf_file_path):
        command = [
            self.dsf_tool_location,
            "--text2dsf",
            txt_file_path,
            dsf_file_path,
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return result.returncode == 0
        except subprocess.CalledProcessError as e:
            logger.error(
                "DSFTool text2dsf failed for %s: %s",
                txt_file_path,
                e.stderr.decode(errors="ignore"),
            )
            return False
        except OSError as e:
            logger.error("Failed to execute DSFTool at %s: %s", self.dsf_tool_location, e)
            return False

    def add_season_to_dsf_txt(self, dsf_to_parse_location, cache_dir):
        # get the name of the dsf to parse
        os.makedirs(cache_dir, exist_ok=True)
        process_dsf_name = os.path.basename(dsf_to_parse_location)
        global_dsf_folder_location = self.get_dsf_folder_location(process_dsf_name)
        ao_overlay_dsf_folder_location = self.get_dsf_folder_location(process_dsf_name, is_overlay=True)
        global_dsf_file_path = os.path.join(global_dsf_folder_location, process_dsf_name)
        ao_overlay_dsf_file_path = os.path.join(ao_overlay_dsf_folder_location, process_dsf_name)

        ao_mesh_dsf_txt_file_path = f"{os.path.join(cache_dir, f"ao_{process_dsf_name}.txt")}"
        global_dsf_txt_file_path = f"{os.path.join(cache_dir, f"global_{process_dsf_name}.txt")}"
        ao_overlay_dsf_txt_file_path = f"{os.path.join(cache_dir, f"ao_overlay_{process_dsf_name}.txt")}"

        skip_main_dsf = False
        if self.convert_dsf_to_txt(dsf_to_parse_location, ao_mesh_dsf_txt_file_path):
            with open(ao_mesh_dsf_txt_file_path, "r") as file:
                for line in file:
                    if line.startswith("RASTER_"):
                        logger.info(f"Found RASTER line, skipping file")
                        skip_main_dsf = True
                        break
        else:
            raise Exception(f"Failed to convert {dsf_to_parse_location} to txt")

        skip_overlay_dsf = False
        if os.path.exists(ao_overlay_dsf_file_path) and self.convert_dsf_to_txt(ao_overlay_dsf_file_path, ao_overlay_dsf_txt_file_path):
            with open(ao_overlay_dsf_txt_file_path, "r") as file:
                for line in file:
                    if line.startswith("RASTER_"):
                        logger.info(f"Found RASTER line, skipping file")
                        skip_overlay_dsf = True      
                        break
        else:
            # If overlay DSF doesn't exist, skip overlay processing
            skip_overlay_dsf = True
            logger.info("Overlay DSF not found or failed to convert; skipping overlay for this tile")

        if skip_main_dsf and skip_overlay_dsf:
            logger.info(f"Skipping {dsf_to_parse_location} because it is already processed")
            return

        raster_refs = []
        on_raster_refs = False
        if self.convert_dsf_to_txt(global_dsf_file_path, global_dsf_txt_file_path):
            with open(global_dsf_txt_file_path, "r") as file:
                for line in file:
                    if line.startswith("RASTER_"):
                        on_raster_refs = True
                        raster_refs.append(line)
                    elif on_raster_refs:
                        break # stop at the first line after the raster refs
                    else:
                        continue
        else:
            raise Exception(f"Failed to convert {global_dsf_file_path} to txt")

        if not raster_refs:
            logger.error(f"Global DSF file {global_dsf_file_path} does not contain any raster refs")
            return

        if not skip_main_dsf:
            with open(ao_mesh_dsf_txt_file_path, "a") as file:
                for raster_ref in raster_refs:
                    file.write(raster_ref)

        if not skip_overlay_dsf:
            with open(ao_overlay_dsf_txt_file_path, "a") as file:
                for raster_ref in raster_refs:
                    file.write(raster_ref)

        temp_mesh_dsf_file_path = f"{os.path.join(cache_dir, f"temp_mesh_{process_dsf_name}")}" 
        temp_overlay_dsf_file_path = f"{os.path.join(cache_dir, f"temp_overlay_{process_dsf_name}")}" 

        if not skip_main_dsf:
            if self.convert_txt_to_dsf(ao_mesh_dsf_txt_file_path, temp_mesh_dsf_file_path):
                logger.info(f"Built temp mesh DSF for {dsf_to_parse_location}")
            else:
                raise Exception(f"Failed to build temp mesh DSF for {dsf_to_parse_location}")

        if not skip_overlay_dsf:
            if self.convert_txt_to_dsf(ao_overlay_dsf_txt_file_path, temp_overlay_dsf_file_path):
                logger.info(f"Built temp overlay DSF for {ao_overlay_dsf_file_path}")
            else:
                raise Exception(f"Failed to build temp overlay DSF for {ao_overlay_dsf_file_path}")

        if not skip_overlay_dsf:
            shutil.copy(ao_overlay_dsf_file_path, ao_overlay_dsf_file_path + ".bak")
            logger.info(f"Backed up old {ao_overlay_dsf_file_path}")
            shutil.move(temp_overlay_dsf_file_path, ao_overlay_dsf_file_path)
            logger.info(f"Moved new {temp_overlay_dsf_file_path} to {ao_overlay_dsf_file_path}")

        if not skip_main_dsf:
            shutil.copy(dsf_to_parse_location, dsf_to_parse_location + ".bak")
            logger.info(f"Backed up old {dsf_to_parse_location}")
            shutil.move(temp_mesh_dsf_file_path, dsf_to_parse_location)
            logger.info(f"Moved new {temp_mesh_dsf_file_path} to {dsf_to_parse_location}")

        shutil.rmtree(cache_dir)
        logger.info(f"Removed cache directory {cache_dir}")

        return

    def scan_for_dsfs(self, scenery_package_path):
        dsf_files_list = []
        dsf_folders = os.path.join(self.ao_path, "z_autoortho", "scenery", scenery_package_path, "Earth Nav Data")
        logger.info(f"Scanning for dsfs in {dsf_folders}")
        if not os.path.isdir(dsf_folders):
            return []
        for folder in os.listdir(dsf_folders):
            if os.path.isdir(os.path.join(dsf_folders, folder)):
                for file in os.listdir(os.path.join(dsf_folders, folder)):
                    if file.endswith(".dsf"):
                        dsf_files_list.append(os.path.join(dsf_folders, folder, file))
        return dsf_files_list

    def add_seasons_to_package(self, scenery_name:str, progress_callback=None, concurent_processes=None):
        # try to load a json file that contains the list of dsfs that have already been processed
        logger.info(f"Adding seasons to {scenery_name}")
        scenery_info_json = os.path.join(self.ao_path, "z_autoortho", scenery_name.replace("z_ao_", "") + "_info.json")
        if os.path.exists(scenery_info_json):
            with open(scenery_info_json, "r") as file:
                scenery_info = json.load(file)
                dsf_files_list = scenery_info.get("dsf_files_list", [])
                pending_dsf_seasons = scenery_info.get("pending_dsf_seasons", [])
        else:
            scenery_info = {}
            dsf_files_list = []
            pending_dsf_seasons = []

        if not dsf_files_list and not pending_dsf_seasons:
            dsf_files_list = self.scan_for_dsfs(scenery_name)
            pending_dsf_seasons = dsf_files_list.copy()
            logger.info(f"Found {len(dsf_files_list)} dsfs and {len(pending_dsf_seasons)} pending dsf seasons in {scenery_name}")

        if concurent_processes is None:
            concurent_processes = min(os.cpu_count(), 8)

        files_done = 0
        failures = 0
        files_total = len(pending_dsf_seasons)

        if progress_callback:
            progress_callback({"pcnt_done": 0, "status": "Processing...", "files_done": files_done, "files_total": files_total})

        with ThreadPoolExecutor(max_workers=concurent_processes) as executor:
            future_to_dsf = {
                executor.submit(
                    self.add_season_to_dsf_txt,
                    dsf_file,
                    os.path.join(self.dsf_dir, str(uuid.uuid4())),
                ): dsf_file
                for dsf_file in list(pending_dsf_seasons)
                if dsf_file in dsf_files_list
            }

            for future in as_completed(future_to_dsf):
                dsf_file = future_to_dsf[future]
                try:
                    future.result()
                    if dsf_file in pending_dsf_seasons:
                        pending_dsf_seasons.remove(dsf_file)
                    files_done += 1
                except Exception as e:
                    logger.error(f"Error adding season to {dsf_file}: {e}")
                    failures += 1
                finally:
                    if progress_callback:
                        pcnt = int((files_done / files_total) * 100) if files_total else 100
                        progress_callback({"pcnt_done": pcnt, "files_done": files_done, "files_total": files_total, "failures": failures})
        
        # only change the keys we need to change
        scenery_info.update({
            "dsf_files_list": dsf_files_list,
            "pending_dsf_seasons": pending_dsf_seasons
        })
        tmp_path = scenery_info_json + ".tmp"
        with open(tmp_path, "w") as file:
            json.dump(scenery_info, file, indent=4)
        os.replace(tmp_path, scenery_info_json)

        return pending_dsf_seasons == []
                    

dsf_utils = DsfUtils()

        

        
    
        
