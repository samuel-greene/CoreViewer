@app.get("/api/scans")
def list_scans(prefix: str = "") -> dict[str, object]:
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    directories = []
    files = []

    try:
        # walk_blobs splits virtual Azure directories on '/'
        for item in container_client.walk_blobs(name_starts_with=prefix, delimiter="/"):
            if getattr(item, "prefix", None):
                dir_name = item.prefix[len(prefix):].rstrip("/")
                directories.append({"name": dir_name, "path": item.prefix})
            else:
                file_name = item.name[len(prefix):]
                if file_name and file_name.lower().endswith((".tif", ".tiff")):
                    files.append({"id": item.name, "name": file_name})
    except Exception as e:
        logger.error("Failed to list blobs: %s", e)
        raise HTTPException(status_code=500, detail="Could not retrieve scans from storage")

    parent_prefix = None
    if prefix:
        parts = prefix.rstrip("/").split("/")
        parent_prefix = "/".join(parts[:-1]) + "/" if len(parts) > 1 else ""

    return {
        "current_path": f"/{prefix}",
        "parent_prefix": parent_prefix,
        "directories": sorted(directories, key=lambda d: d["name"].lower()),
        "files": sorted(files, key=lambda f: f["name"].lower()),
    }