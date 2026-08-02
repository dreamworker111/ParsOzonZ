import json
import re
from typing import Any

SKIP_NAMES = {"ещё", "еще", "все", "all", "показать все", "свернуть", "развернуть"}

# i18n / internal keys that are not real category names
INVALID_NAME_RE = re.compile(
    r"(^|[.\s])(profile|widget|web[A-Z]|myProfile|layout|state|config|token|session)([.\s]|$)",
    re.I,
)
CAMELCASE_KEY_RE = re.compile(r"^[a-z]+[A-Z][a-zA-Z0-9]*$")
DOT_KEY_RE = re.compile(r"^[a-zA-Z_]+\.[a-zA-Z_.]+$")


def is_valid_category_id(category_id: str) -> bool:
    """Ozon seller categories use numeric IDs."""
    return bool(re.fullmatch(r"\d{2,}", str(category_id).strip()))


def is_valid_category_name(name: str) -> bool:
    name = str(name).strip()
    if not name or len(name) < 2 or len(name) > 80:
        return False
    if name.lower() in SKIP_NAMES:
        return False
    if not re.search(r"[а-яА-ЯёЁa-zA-Z0-9]", name):
        return False
    if DOT_KEY_RE.match(name):
        return False
    if CAMELCASE_KEY_RE.match(name):
        return False
    if INVALID_NAME_RE.search(name):
        return False
    if "." in name and not re.search(r"[а-яА-ЯёЁ]", name):
        return False
    return True


def is_valid_category(name: str, category_id: str) -> bool:
    return is_valid_category_id(category_id) and is_valid_category_name(name)


def extract_json_blocks(html: str) -> list[Any]:
    results: list[Any] = []
    for pattern in (
        r'<script[^>]*type="application/json"[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
        r"window\.__NUXT__\s*=\s*(.*?);</script>",
    ):
        for match in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            raw = match.group(1).strip()
            try:
                results.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return results


def _category_from_dict(obj: dict) -> dict | None:
    name = (
        obj.get("title")
        or obj.get("name")
        or obj.get("caption")
        or obj.get("text")
        or obj.get("label")
    )
    cid = obj.get("categoryId") or obj.get("category_id") or obj.get("id") or obj.get("value")

    if isinstance(name, dict):
        name = name.get("text") or name.get("title")
    if not name or cid is None:
        return None
    if not isinstance(name, str):
        return None

    name = name.strip()
    cid = str(cid).strip()

    if not is_valid_category(name, cid):
        return None

    child_keys = ("children", "categories", "nodes", "items", "sections", "subCategories", "subcategories")
    children: list[dict] = []
    for key in child_keys:
        val = obj.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    child = _normalize_category_item(item)
                    if child:
                        children.append(child)

    return {"id": cid, "name": name, "children": children}


def _normalize_category_item(obj: dict) -> dict | None:
    node = _category_from_dict(obj)
    if node:
        return node

    link = obj.get("link") or obj.get("deeplink") or obj.get("url") or ""
    name = obj.get("title") or obj.get("text") or obj.get("name") or ""
    if not isinstance(name, str):
        return None
    name = name.strip()

    match = re.search(r"[?&]category=(\d+)", str(link), re.I)
    if not match:
        match = re.search(r"category[=/](\d+)", str(link), re.I)
    if match and is_valid_category(name, match.group(1)):
        return {"id": match.group(1), "name": name, "url": str(link) or None, "children": []}
    return None


def find_category_nodes(obj: Any, found: list[dict] | None = None) -> list[dict]:
    if found is None:
        found = []

    if isinstance(obj, dict):
        node = _normalize_category_item(obj)
        if node and not any(n["id"] == node["id"] for n in found):
            found.append(node)
        for value in obj.values():
            find_category_nodes(value, found)
    elif isinstance(obj, list):
        for item in obj:
            find_category_nodes(item, found)

    return found


def parse_composer_response(data: dict) -> list[dict]:
    categories: list[dict] = []
    widget_states = data.get("widgetStates") or {}

    for key, raw in widget_states.items():
        key_lower = key.lower()
        if not any(token in key_lower for token in ("filter", "category", "catalog")):
            continue
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        categories.extend(find_category_nodes(state))

    return _build_tree(categories)


def _build_tree(flat: list[dict]) -> list[dict]:
    if not flat:
        return []

    merged: dict[str, dict] = {}
    for item in flat:
        if not is_valid_category(item["name"], item["id"]):
            continue
        cid = str(item["id"])
        if cid not in merged:
            merged[cid] = {"id": cid, "name": item["name"], "url": item.get("url"), "children": []}
        elif item.get("url") and not merged[cid].get("url"):
            merged[cid]["url"] = item["url"]
        for child in item.get("children", []):
            if not is_valid_category(child["name"], child["id"]):
                continue
            child_id = str(child["id"])
            if child_id not in merged:
                merged[child_id] = {
                    "id": child_id,
                    "name": child["name"],
                    "url": child.get("url"),
                    "children": [],
                }
            elif child.get("url") and not merged[child_id].get("url"):
                merged[child_id]["url"] = child["url"]
            if not any(c["id"] == child_id for c in merged[cid]["children"]):
                merged[cid]["children"].append(merged[child_id])

    child_ids = {c["id"] for node in merged.values() for c in node.get("children", [])}
    roots = [node for cid, node in merged.items() if cid not in child_ids]
    return roots or list(merged.values())


def merge_category_trees(trees: list[list[dict]]) -> list[dict]:
    flat: list[dict] = []
    for tree in trees:
        flat.extend(tree)
    return _build_tree(flat)


def find_category_node(nodes: list[dict], category_id: str) -> dict | None:
    category_id = str(category_id)
    for node in nodes:
        if str(node.get("id", "")) == category_id:
            return node
        if found := find_category_node(node.get("children") or [], category_id):
            return found
    return None


def extract_direct_children(
    tree: list[dict],
    parent_id: str | None,
    *,
    root_ids: set[str] | None = None,
    page_category_id: str | None = None,
) -> list[dict]:
    """Direct children of parent_id from a parsed category tree."""
    root_ids = root_ids or set()
    if parent_id is None:
        return [
            {"id": str(n["id"]), "name": n["name"], "url": n.get("url"), "children": []}
            for n in tree
            if is_valid_category(str(n.get("name", "")), str(n.get("id", "")))
        ]

    parent_id = str(parent_id)
    parent = find_category_node(tree, parent_id)
    if parent:
        children = parent.get("children") or []
        if children:
            return [
                {"id": str(c["id"]), "name": c["name"], "url": c.get("url"), "children": []}
                for c in children
                if is_valid_category(str(c.get("name", "")), str(c.get("id", "")))
            ]

    # No parent means no proven parent-child relation. Returning unrelated
    # categories here caused every root category to be copied into branches.
    return []


def collect_leaf_category_ids(nodes: list[dict]) -> list[str]:
    """Return leaf category IDs for parsing — avoids duplicate parent/child runs."""
    ids: list[str] = []

    def walk(node: dict) -> None:
        children = [c for c in node.get("children", []) if is_valid_category(c["name"], c["id"])]
        if children:
            for child in children:
                walk(child)
        elif is_valid_category(node["name"], node["id"]):
            ids.append(str(node["id"]))

    for node in nodes:
        walk(node)
    return ids
