def int_param(cfg: dict, name: str) -> int:
    v = cfg.get("params", {}).get(name)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f'rule {cfg["id"]}: param "{name}" must be a positive integer')
    return v


def amount_param(cfg: dict, name: str) -> int:
    """Money params are minor units, given as integer or digit string."""
    v = cfg.get("params", {}).get(name)
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).isdigit():
        raise ValueError(f'rule {cfg["id"]}: param "{name}" must be a non-negative integer (minor units)')
    return int(v)
