"""Mappers used in autoortho code"""

def map_kubilus_region_to_simheaven_region(region_id: str) -> str:
    """Map which SimHeaven region a Kubilus region belongs to"""
    region_map = {
        "na": "America",
        "eur": "Europe",
        "asi": "Asia",
        "afr": "Africa",
        "aus_pac": "Australia-Oceania",
        "sa": "America",
    }
    return region_map[region_id]
