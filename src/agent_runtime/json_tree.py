"""Lossless JSON trees: unchanged values share verified content-addressed nodes.

Nodes are explicitly tagged, so user JSON cannot impersonate a reference. There
is no predecessor chain: any checkpoint can be restored directly from its root.
"""

from __future__ import annotations

import hashlib
import json

from src.harness_contracts import HashedBlobRef

MEDIA_TYPE = "application/vnd.agent-runtime.json-tree+json"


def store_json_tree(value, store):
    from .store import json_bytes

    wire = json_bytes(value)
    if len(wire) <= 1024:
        return store.put_bytes(wire, "application/json")

    owner = getattr(store, "_root", store)
    if not hasattr(owner, "_json_tree_nodes"):
        owner._json_tree_nodes = {}
    cache = owner._json_tree_nodes

    def encode(item):
        raw = json_bytes(item)
        key = hashlib.sha256(raw).hexdigest()
        if key in cache:
            return cache[key]
        if len(raw) <= 1024 or not isinstance(item, (dict, list)):
            # Cached nodes must not alias the caller's mutable checkpoint state.
            node = ["value", json.loads(raw)]
        elif isinstance(item, dict):
            node = ["object", [[key, encode(child)] for key, child in sorted(item.items())]]
        else:
            node = ["array", [encode(child) for child in item]]
        encoded = json_bytes(node)
        if len(encoded) >= 512:
            node = ["ref", store.put_bytes(encoded, "application/json").model_dump(mode="json")]
        if len(cache) >= 4096:
            cache.clear()
        cache[key] = node
        return node

    root = {"schema_version": 1, "tree": encode(value),
            "wire_sha256": hashlib.sha256(wire).hexdigest()}
    return store.put_bytes(json_bytes(root), MEDIA_TYPE)


def read_json_tree(ref, read_blob):
    """Read a tree or historical plain JSON, checking every referenced hash."""
    from .store import json_bytes

    document = json.loads(read_blob(ref))
    if ref.media_type != MEDIA_TYPE:
        return document
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported JSON tree version")
    active = set()

    def decode(node):
        if not isinstance(node, list) or len(node) != 2:
            raise ValueError("invalid JSON tree node")
        kind, item = node
        if kind == "value":
            return item
        if kind == "ref":
            child = HashedBlobRef.model_validate(item)
            if child.sha256 in active:
                raise ValueError("cyclic JSON tree reference")
            active.add(child.sha256)
            try:
                return decode(json.loads(read_blob(child)))
            finally:
                active.remove(child.sha256)
        if kind == "array" and isinstance(item, list):
            return [decode(child) for child in item]
        if kind == "object" and isinstance(item, list):
            result = {}
            for pair in item:
                if (not isinstance(pair, list) or len(pair) != 2
                        or not isinstance(pair[0], str) or pair[0] in result):
                    raise ValueError("invalid JSON tree object entry")
                result[pair[0]] = decode(pair[1])
            return result
        raise ValueError("unknown JSON tree node")

    value = decode(document["tree"])
    if hashlib.sha256(json_bytes(value)).hexdigest() != document.get("wire_sha256"):
        raise ValueError("reconstructed JSON tree hash mismatch")
    return value
