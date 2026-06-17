import hashlib
import json
import os
from typing import Any, Dict, List, Tuple

import yaml


def load_manifest(path: str) -> Tuple[List[Dict[str, Any]], str]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"清单文件不存在: {path}")

    ext = os.path.splitext(path)[1].lower()
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if ext == ".json":
        data = json.loads(content)
    elif ext in (".yaml", ".yml"):
        data = yaml.safe_load(content)
    elif ext in (".csv",):
        data = _parse_csv(path)
    else:
        raise ValueError(f"不支持的清单格式: {ext}，支持 .json / .yaml / .yml / .csv")

    items = _normalize_manifest(data)
    manifest_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return items, manifest_hash


def _parse_csv(path: str) -> Any:
    import csv

    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k.strip(): (v.strip() if v else None) for k, v in row.items()})
    return {"items": rows}


def _normalize_manifest(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "items" in data and isinstance(data["items"], list):
            items = data["items"]
        elif "packages" in data and isinstance(data["packages"], list):
            items = data["packages"]
        elif "patches" in data and isinstance(data["patches"], list):
            items = data["patches"]
        else:
            items = [data]
    else:
        raise ValueError("清单格式错误：顶层必须是列表或包含 items/packages/patches 字段的对象")

    normalized: List[Dict[str, Any]] = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {idx + 1} 条清单条目格式错误，必须是对象")
        item = {
            "package_name": raw.get("package_name") or raw.get("name") or raw.get("pkg") or "",
            "version": raw.get("version") or raw.get("ver") or None,
            "source_path": raw.get("source_path") or raw.get("path") or raw.get("src") or None,
            "checksum": raw.get("checksum") or raw.get("sha256") or raw.get("md5") or None,
            "metadata": {},
        }
        known_keys = {"package_name", "name", "pkg", "version", "ver", "source_path", "path", "src", "checksum", "sha256", "md5"}
        for k, v in raw.items():
            if k not in known_keys:
                item["metadata"][k] = v
        normalized.append(item)
    return normalized
