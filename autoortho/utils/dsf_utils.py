"""module to handle dsf files"""
import os
import json
import shutil
import subprocess
import uuid
from logging import getLogger
from concurrent.futures import ThreadPoolExecutor

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
        return os.path.join("autoortho", "lib", lib_subfolder, "DSFTool.exe" if system_type == "windows" else "DSFTool")

    def get_dsf_folder_location(self, dsf_name, is_overlay=False):
        # remove .dsf from the end of the dsf name
        dsf_name = dsf_name.rstrip(".dsf")
        lat = str(dsf_name[:3])
        lon = str(dsf_name[3:])
        positive_lat = lat.startswith("+")
        positive_lon = lon.startswith("+")
        lat = lat.lstrip("+")
        lon = lon.lstrip("+")
        lat = float(lat)
        lon = float(lon)

        folder_lat = int(lat // self.GRID) * self.GRID
        folder_lon = int(lon // self.GRID) * self.GRID
        if positive_lat:
            folder_lat = "+" + str(folder_lat)
        if positive_lon:
            folder_lon = "+" + str(folder_lon)
        if is_overlay:
            return os.path.join(self.overlay_scenery_path, str(folder_lat), str(folder_lon))
        else:
            return os.path.join(self.global_scenery_path, str(folder_lat), str(folder_lon))


    
    def convert_dsf_to_txt(self, dsf_file_path, txt_file_path):
        command = [self.dsf_tool_location, "--dsf2text", dsf_file_path, txt_file_path]
        output = subprocess.run(command, shell=True)
        return output.returncode == 0

    def convert_txt_to_dsf(self, txt_file_path, dsf_file_path):
        command = [self.dsf_tool_location, "--text2dsf", txt_file_path, dsf_file_path]
        output = subprocess.run(command, shell=True)
        return output.returncode == 0

    def add_season_to_dsf_txt(self, dsf_to_parse_location, cache_dir):
        # get the name of the dsf to parse
        process_dsf_name = os.path.basename(dsf_to_parse_location)
        global_dsf_folder_location = self.get_dsf_folder_location(process_dsf_name)
        ao_overlay_dsf_folder_location = self.get_dsf_folder_location(process_dsf_name, is_overlay=True)

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
        if self.convert_dsf_to_txt(ao_overlay_dsf_folder_location, ao_overlay_dsf_txt_file_path):
            with open(ao_overlay_dsf_txt_file_path, "r") as file:
                for line in file:
                    if line.startswith("RASTER_"):
                        logger.info(f"Found RASTER line, skipping file")
                        skip_overlay_dsf = True      
                        break
        else:
            raise Exception(f"Failed to convert {ao_overlay_dsf_folder_location} to txt")

        if skip_main_dsf and skip_overlay_dsf:
            logger.info(f"Skipping {dsf_to_parse_location} because it is already processed")
            return

        raster_refs = []
        on_raster_refs = False
        if self.convert_dsf_to_txt(global_dsf_folder_location, global_dsf_txt_file_path):
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
            raise Exception(f"Failed to convert {global_dsf_folder_location} to txt")

        if not raster_refs:
            logger.error(f"Global DSF file {global_dsf_folder_location} does not contain any raster refs")
            return

        if not skip_main_dsf:
            with open(ao_mesh_dsf_txt_file_path, "a") as file:
                for raster_ref in raster_refs:
                    file.write(raster_ref)

        if not skip_overlay_dsf:
            with open(ao_overlay_dsf_txt_file_path, "a") as file:
                for raster_ref in raster_refs:
                    file.write(raster_ref)

        temp_dsf_file_path = f"{os.path.join(cache_dir, f"temp_{process_dsf_name}.dsf")}"
        if self.convert_txt_to_dsf(ao_mesh_dsf_txt_file_path, temp_dsf_file_path):
            logger.info(f"Added season to {dsf_to_parse_location}")
        else:
            raise Exception(f"Failed to add season to {dsf_to_parse_location}")

        if not skip_overlay_dsf:
            shutil.copy(ao_overlay_dsf_folder_location, ao_overlay_dsf_folder_location + ".bak")
            logger.info(f"Backed up old {ao_overlay_dsf_folder_location}")
            shutil.move(temp_dsf_file_path, ao_overlay_dsf_folder_location)
            logger.info(f"Moved new {temp_dsf_file_path} to {ao_overlay_dsf_folder_location}")

        if not skip_main_dsf:
            shutil.copy(dsf_to_parse_location, dsf_to_parse_location + ".bak")
            logger.info(f"Backed up old {dsf_to_parse_location}")
            shutil.move(temp_dsf_file_path, dsf_to_parse_location)
            logger.info(f"Moved new {temp_dsf_file_path} to {dsf_to_parse_location}")

        shutil.rmtree(cache_dir)
        logger.info(f"Removed cache directory {cache_dir}")

        return



    def scan_for_dsfs(self, scenery_package_path):
        dsf_files_list = []
        dsf_folders = os.path.join(scenery_package_path, "Earth Nav Data")
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
            for dsf_file in dsf_files_list:
                if dsf_file in pending_dsf_seasons:
                    cache_dir = os.path.join(self.dsf_dir, str(uuid.uuid4()))
                    for future in executor.map(self.add_season_to_dsf_txt, dsf_file, cache_dir):
                        try:
                            future.result()
                            pending_dsf_seasons.remove(dsf_file)
                            files_done += 1
                            if progress_callback:
                                progress_callback({"pcnt_done": int((files_done / files_total) * 100), "files_done": files_done, "files_total": files_total, "failures": failures})
                        except Exception as e:
                            logger.error(f"Error adding season to {dsf_file}: {e}")
                            failures += 1
                            if progress_callback:
                                progress_callback({"pcnt_done": int((files_done / files_total) * 100), "files_done": files_done, "files_total": files_total, "failures": failures})
        
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

        

        
    
        
