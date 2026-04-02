from importlib import metadata


def get_package_version(package_name: str) -> str:
    """Get the version of the given package."""
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        raise RuntimeError(f"Package {package_name} not found.")
