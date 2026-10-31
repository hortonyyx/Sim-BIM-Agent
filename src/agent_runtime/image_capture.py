"""Lossless JSON captures with repeated image bytes stored once by digest.

Templates use the same canonical serializer as the wire adapter. Only protocol
image strings are replaced; arbitrary text, tool arguments and URLs stay intact.
"""

from __future__ import annotations

import base64
import hashlib
import json

from src.harness_contracts import EncodedImageReference, ImageReferencedCapture


def capture_images(value, store, wire: bytes):
    from .store import json_bytes

    template = json.loads(wire)
    images = []

    def replace(parent, key, pointer, mime, prefix=""):
        encoded = parent[key][len(prefix):]
        raw = base64.b64decode(encoded, validate=True)
        image = store.put_bytes(raw, mime)
        exact = None
        if base64.b64encode(raw).decode("ascii") != encoded:
            exact = store.put_bytes(encoded.encode("ascii"), "text/plain")
        images.append(EncodedImageReference(location=pointer, image=image,
                                            prefix=prefix, base64_text=exact))
        parent[key] = None

    def walk(node, pointer=""):
        if isinstance(node, dict):
            if node.get("type") == "image_url" and isinstance(node.get("image_url"), dict):
                url = node["image_url"].get("url", "")
                if isinstance(url, str) and url.startswith("data:image/") and ";base64," in url:
                    prefix = url.split(",", 1)[0] + ","
                    replace(node["image_url"], "url", pointer + "/image_url/url",
                            prefix[5:].split(";", 1)[0], prefix)
            elif node.get("type") == "image":
                source = node.get("source")
                if isinstance(source, dict) and source.get("type") == "base64":
                    replace(source, "data", pointer + "/source/data", source["media_type"])
                elif isinstance(node.get("data"), str) and "mimeType" in node:
                    replace(node, "data", pointer + "/data", node["mimeType"])
            for key, child in node.items():
                walk(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, pointer + f"/{i}")

    walk(template)
    if not images:
        return None
    return ImageReferencedCapture(blob=store.put_json_tree(template),
        images=tuple(images), wire_sha256=hashlib.sha256(wire).hexdigest())


def reconstruct_capture(capture, read_blob) -> bytes:
    """Verify template, image and reconstructed wire hashes before returning."""
    from .store import json_bytes

    from .json_tree import read_json_tree
    value = read_json_tree(capture.blob, read_blob)
    seen = set()
    for item in capture.images:
        if item.location in seen or not item.location.startswith("/"):
            raise ValueError("duplicate or invalid image substitution location")
        seen.add(item.location)
        keys = [p.replace("~1", "/").replace("~0", "~") for p in item.location[1:].split("/")]
        parent = value
        for key in keys[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        key = int(keys[-1]) if isinstance(parent, list) else keys[-1]
        if parent[key] is not None:
            raise ValueError("image substitution does not point to an empty template slot")
        raw = read_blob(item.image)
        encoded = (read_blob(item.base64_text).decode("ascii") if item.base64_text else
                   base64.b64encode(raw).decode("ascii"))
        if base64.b64decode(encoded, validate=True) != raw:
            raise ValueError("stored base64 spelling differs from image bytes")
        parent[key] = item.prefix + encoded
    wire = json_bytes(value)
    if hashlib.sha256(wire).hexdigest() != capture.wire_sha256:
        raise ValueError("reconstructed wire hash mismatch")
    return wire
