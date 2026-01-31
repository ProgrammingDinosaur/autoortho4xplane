"""Module to handle .ter terrain files for SUPER_ROUGHNESS patching"""
import os
import json
from logging import getLogger
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from enum import Enum

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

log = getLogger(__name__)


class RoughnessPatchResult(Enum):
    """Result of patching a single .ter file"""
    ADDED = "added"
    UPDATED = "updated"
    FAILED = "failed"


class TerUtils:
    """Utilities for patching .ter files with SUPER_ROUGHNESS parameter"""

    def __init__(self):
        self.ao_path = CFG.paths.scenery_path

    def get_terrain_folder(self, scenery_name: str) -> str:
        """Get the terrain folder path for a scenery package"""
        return os.path.join(
            self.ao_path, "z_autoortho", "scenery", scenery_name, "terrain"
        )

    def scan_for_ter_files(self, scenery_name: str) -> tuple[dict, int]:
        """
        Scan terrain folder for .ter files.

        Returns:
            tuple: (dict of terrain files by subfolder, total count)
        """
        total_ters = 0
        ter_files = {}
        terrain_folder = self.get_terrain_folder(scenery_name)

        log.debug(f"Scanning for .ter files in {terrain_folder}")

        if not os.path.isdir(terrain_folder):
            log.warning(f"Terrain folder does not exist: {terrain_folder}")
            return {}, 0

        # .ter files are typically in the terrain folder directly
        for item in os.listdir(terrain_folder):
            item_path = os.path.join(terrain_folder, item)

            if os.path.isfile(item_path) and item.endswith(".ter"):
                # Files directly in terrain folder
                if "" not in ter_files:
                    ter_files[""] = []
                ter_files[""].append(item)
                total_ters += 1
            elif os.path.isdir(item_path):
                # Subfolder - scan it too
                for file in os.listdir(item_path):
                    if file.endswith(".ter"):
                        if item not in ter_files:
                            ter_files[item] = []
                        ter_files[item].append(file)
                        total_ters += 1

        log.debug(f"Found {total_ters} .ter files in {scenery_name}")
        return ter_files, total_ters

    def patch_ter_file(
        self,
        ter_path: str,
        roughness_value: float,
        processed_ter_roughness: dict,
        subfolder: str,
        filename: str
    ) -> RoughnessPatchResult:
        """
        Add or update SUPER_ROUGHNESS in a single .ter file.

        Args:
            ter_path: Full path to the .ter file
            roughness_value: The SUPER_ROUGHNESS value to set (0.0 to 1.0)
            processed_ter_roughness: Dict tracking processed files
            subfolder: Subfolder key for tracking
            filename: Filename for tracking

        Returns:
            RoughnessPatchResult: ADDED, UPDATED, or FAILED
        """
        # Check if already processed with same value
        if subfolder in processed_ter_roughness:
            processed_info = processed_ter_roughness[subfolder]
            if isinstance(processed_info, dict) and filename in processed_info:
                existing_value = processed_info[filename]
                if existing_value == roughness_value:
                    log.debug(f"TER {ter_path} already has value {roughness_value}")
                    return RoughnessPatchResult.ADDED

        try:
            with open(ter_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            log.error(f"Failed to read {ter_path}: {e}")
            return RoughnessPatchResult.FAILED

        # Check if already has SUPER_ROUGHNESS
        roughness_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith('SUPER_ROUGHNESS'):
                roughness_idx = i
                break

        new_line = f"SUPER_ROUGHNESS {roughness_value}\n"
        result = RoughnessPatchResult.ADDED

        if roughness_idx is not None:
            # Update existing value
            lines[roughness_idx] = new_line
            result = RoughnessPatchResult.UPDATED
        else:
            # Append new line (ensure there's a newline before if needed)
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines.append(new_line)

        try:
            with open(ter_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            log.debug(f"Patched {ter_path} with SUPER_ROUGHNESS {roughness_value}")
            return result
        except Exception as e:
            log.error(f"Failed to write {ter_path}: {e}")
            return RoughnessPatchResult.FAILED

    def _patch_single_file(
        self,
        scenery_name: str,
        subfolder: str,
        filename: str,
        roughness_value: float,
        processed_ter_roughness: dict
    ) -> RoughnessPatchResult:
        """Internal method to patch a single file with full path construction"""
        terrain_folder = self.get_terrain_folder(scenery_name)
        if subfolder:
            ter_path = os.path.join(terrain_folder, subfolder, filename)
        else:
            ter_path = os.path.join(terrain_folder, filename)

        return self.patch_ter_file(
            ter_path, roughness_value, processed_ter_roughness, subfolder, filename
        )

    def patch_terrain_to_package(
        self,
        scenery_name: str,
        roughness_value: float,
        progress_callback=None
    ) -> bool:
        """
        Patch all .ter files in a scenery package with SUPER_ROUGHNESS.

        Args:
            scenery_name: Name of the scenery package (e.g., "z_ao_na")
            roughness_value: The SUPER_ROUGHNESS value to set (0.0 to 1.0)
            progress_callback: Optional callback for progress updates

        Returns:
            bool: True if all files were patched successfully
        """
        log.info(f"Patching {scenery_name} with SUPER_ROUGHNESS {roughness_value}")

        # Load existing scenery info
        scenery_info_json = os.path.join(
            self.ao_path, "z_autoortho",
            scenery_name.replace("z_ao_", "") + "_info.json"
        )

        if os.path.exists(scenery_info_json):
            with open(scenery_info_json, "r") as file:
                scenery_info = json.load(file)
                ter_files = scenery_info.get("ter_files", {})
                processed_ter_roughness = scenery_info.get(
                    "processed_ter_roughness", {}
                )
                total_ters = scenery_info.get("total_ters", 0)
        else:
            scenery_info = {}
            ter_files = {}
            total_ters = 0
            processed_ter_roughness = {}

        # Scan for files if not already done
        if not ter_files:
            ter_files, total_ters = self.scan_for_ter_files(scenery_name)
            log.info(f"Found {total_ters} .ter files in {scenery_name}")

        if total_ters == 0:
            log.warning(f"No .ter files found in {scenery_name}")
            return True

        files_done = 0
        failures = 0
        workers = int(getattr(CFG.terrain, 'ter_patch_workers', 8))

        if progress_callback:
            progress_callback({
                "pcnt_done": 0,
                "status": "Patching terrain files...",
                "files_done": files_done,
                "files_total": total_ters
            })

        # Stream tasks into the executor
        def _iter_tasks():
            for subfolder, files in ter_files.items():
                for filename in files:
                    yield subfolder, filename

        with ThreadPoolExecutor(max_workers=workers) as executor:
            tasks = _iter_tasks()
            in_flight = {}

            # Prime up to max workers
            try:
                for _ in range(max(1, workers)):
                    subfolder, filename = next(tasks)
                    fut = executor.submit(
                        self._patch_single_file,
                        scenery_name,
                        subfolder,
                        filename,
                        roughness_value,
                        processed_ter_roughness,
                    )
                    in_flight[fut] = (subfolder, filename)
            except StopIteration:
                pass

            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    subfolder, filename = in_flight.pop(future)
                    try:
                        result = future.result()
                        if result in (RoughnessPatchResult.ADDED,
                                      RoughnessPatchResult.UPDATED):
                            # Track as processed with the value applied
                            if subfolder not in processed_ter_roughness:
                                processed_ter_roughness[subfolder] = {}
                            processed_ter_roughness[subfolder][filename] = \
                                roughness_value
                        else:
                            log.error(f"Failed to patch {subfolder}/{filename}")
                            failures += 1
                        files_done += 1
                    except Exception as e:
                        log.error(f"Error patching {subfolder}/{filename}: {e}")
                        failures += 1
                        files_done += 1
                    finally:
                        if progress_callback:
                            pcnt = int((files_done / total_ters) * 100) \
                                if total_ters else 100
                            progress_callback({
                                "pcnt_done": pcnt,
                                "files_done": files_done,
                                "files_total": total_ters,
                                "failures": failures
                            })

                    # Keep the pipeline full
                    try:
                        subfolder, filename = next(tasks)
                        fut = executor.submit(
                            self._patch_single_file,
                            scenery_name,
                            subfolder,
                            filename,
                            roughness_value,
                            processed_ter_roughness,
                        )
                        in_flight[fut] = (subfolder, filename)
                    except StopIteration:
                        pass

        # Update scenery info
        scenery_info.update({
            "ter_files": ter_files,
            "processed_ter_roughness": processed_ter_roughness,
            "total_ters": total_ters,
            "roughness_value": roughness_value
        })

        tmp_path = scenery_info_json + ".tmp"
        with open(tmp_path, "w") as file:
            json.dump(scenery_info, file, indent=4)
        os.replace(tmp_path, scenery_info_json)

        # Check if all files were processed
        total_processed = sum(
            len(files) for files in processed_ter_roughness.values()
        )

        return total_processed >= total_ters and failures == 0

    def remove_roughness_from_file(self, ter_path: str) -> bool:
        """
        Remove SUPER_ROUGHNESS line from a .ter file.

        Args:
            ter_path: Full path to the .ter file

        Returns:
            bool: True if successful
        """
        try:
            with open(ter_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            log.error(f"Failed to read {ter_path}: {e}")
            return False

        # Filter out SUPER_ROUGHNESS lines
        new_lines = [
            line for line in lines
            if not line.strip().startswith('SUPER_ROUGHNESS')
        ]

        if len(new_lines) == len(lines):
            # No SUPER_ROUGHNESS found, nothing to do
            log.debug(f"No SUPER_ROUGHNESS found in {ter_path}")
            return True

        try:
            with open(ter_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            log.debug(f"Removed SUPER_ROUGHNESS from {ter_path}")
            return True
        except Exception as e:
            log.error(f"Failed to write {ter_path}: {e}")
            return False

    def restore_ter_files(self, scenery_name: str, progress_callback=None) -> bool:
        """
        Remove SUPER_ROUGHNESS from all patched .ter files.

        Args:
            scenery_name: Name of the scenery package
            progress_callback: Optional callback for progress updates

        Returns:
            bool: True if all files were restored successfully
        """
        log.info(f"Restoring terrain files in {scenery_name}")

        scenery_info_json = os.path.join(
            self.ao_path, "z_autoortho",
            scenery_name.replace("z_ao_", "") + "_info.json"
        )

        if not os.path.exists(scenery_info_json):
            log.error(f"Scenery info json does not exist for {scenery_name}")
            return False

        with open(scenery_info_json, "r") as file:
            scenery_info = json.load(file)
            processed_ter_roughness = scenery_info.get(
                "processed_ter_roughness", {}
            )
            total_ters = scenery_info.get("total_ters", 0)

        if not processed_ter_roughness:
            log.info(f"No SUPER_ROUGHNESS patches to restore for {scenery_name}")
            return True

        files_done = 0
        failures = 0
        terrain_folder = self.get_terrain_folder(scenery_name)

        if progress_callback:
            progress_callback({
                "pcnt_done": 0,
                "status": "Removing SUPER_ROUGHNESS...",
                "files_done": files_done,
                "files_total": total_ters
            })

        for subfolder, files_dict in processed_ter_roughness.items():
            if isinstance(files_dict, dict):
                files = files_dict.keys()
            else:
                # Backward compatibility if it was stored as a list
                files = files_dict

            for filename in files:
                if subfolder:
                    ter_path = os.path.join(terrain_folder, subfolder, filename)
                else:
                    ter_path = os.path.join(terrain_folder, filename)

                if os.path.exists(ter_path):
                    if not self.remove_roughness_from_file(ter_path):
                        failures += 1
                else:
                    log.warning(f"File not found during restore: {ter_path}")

                files_done += 1
                if progress_callback:
                    pcnt = int((files_done / total_ters) * 100) \
                        if total_ters else 100
                    progress_callback({
                        "pcnt_done": pcnt,
                        "files_done": files_done,
                        "files_total": total_ters
                    })

        # Reset fields in scenery info json
        scenery_info.update({
            "processed_ter_roughness": {},
            "roughness_value": None
        })

        tmp_path = scenery_info_json + ".tmp"
        with open(tmp_path, "w") as file:
            json.dump(scenery_info, file, indent=4)
        os.replace(tmp_path, scenery_info_json)

        return failures == 0


# Singleton instance
ter_utils = TerUtils()
