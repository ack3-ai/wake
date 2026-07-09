import hashlib
import logging
import platform
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union
from zipfile import ZipFile

import aiohttp
from Crypto.Hash import keccak
from pydantic import BaseModel, Field, ValidationError

from wake.config import UnsupportedPlatformError, WakeConfig
from wake.core import get_logger
from wake.core.solidity_version import SolidityVersion
from wake.utils.version import get_package_version

from .abc import CompilerVersionManagerAbc
from .exceptions import ChecksumError, UnsupportedVersionError

logger = get_logger(__name__)


class SolcBuildInfo(BaseModel):
    path: Optional[str] = None
    version: SolidityVersion
    build: Optional[str] = None
    long_version: Optional[SolidityVersion] = Field(default=None, alias="longVersion")
    keccak256: Optional[str] = None
    sha256: str
    urls: Optional[List[str]] = None


class SolcBuilds(BaseModel):
    builds: List[SolcBuildInfo]
    releases: Dict[SolidityVersion, str]
    latest_release: Optional[str] = Field(default=None, alias="latestRelease")


class _SolcSource:
    """
    A single source of ``solc`` binaries: a ``list.json`` index plus one or more
    mirror download bases that all serve the same set of files. ``third_party``
    marks non-official sources (currently only the nikitastupin aarch64 mirror,
    used as a backup for older linux-arm64 versions the official repository does
    not provide).
    """

    list_urls: List[str]
    download_urls: List[str]
    cache_path: Path
    third_party: bool
    builds: Optional[SolcBuilds]

    def __init__(
        self,
        list_urls: List[str],
        download_urls: List[str],
        cache_path: Path,
        third_party: bool,
    ):
        self.list_urls = list_urls
        self.download_urls = download_urls
        self.cache_path = cache_path
        self.third_party = third_party
        self.builds = None


class SolcVersionManager(CompilerVersionManagerAbc):
    """
    Solc version manager that can install, remove and provide info about `solc` compiler.
    """

    # TODO: Add support for older solc versions in svm module
    #  Currently only builds present in binaries.soliditylang.org repository are supported.
    #  We should also support older solc releases.
    # assignees: michprev

    BINARIES_URL: str = "https://binaries.soliditylang.org"
    GITHUB_URL: str = "https://raw.githubusercontent.com/ethereum/solc-bin/gh-pages"
    # official solc-bin GitHub mirror (the project moved to the argotorg org); this
    # is the only GitHub mirror publishing linux-arm64 binaries
    ARGOTORG_GITHUB_URL: str = (
        "https://raw.githubusercontent.com/argotorg/solc-bin/gh-pages"
    )
    # 3rd-party backup mirror providing older linux aarch64 builds not published by
    # the official repository
    NIKITA_LINUX_AARCH64_URL: str = (
        "https://raw.githubusercontent.com/nikitastupin/solc/main"
    )
    INSTALL_RETRY_COUNT: int = 5

    __platform: str
    __sources: List[_SolcSource]
    __compilers_path: Path
    __solc_builds: Optional[SolcBuilds]
    __version_source: Dict[SolidityVersion, _SolcSource]
    __list_force_loaded: bool
    __headers: Dict[str, str]

    def __init__(self, wake_config: WakeConfig):
        system = platform.system()
        machine = platform.machine()
        amd64 = {"x86_64", "amd64", "AMD64", "x86-64"}
        arm64 = {"aarch64", "arm64", "AARCH64", "ARM64"}

        if system == "Linux" and machine in amd64:
            self.__platform = "linux-amd64"
        elif system == "Linux" and machine in arm64:
            self.__platform = "linux-aarch64"
        elif system == "Darwin" and machine in amd64.union(arm64):
            self.__platform = "macosx-amd64"
        elif system == "Windows" and machine in amd64.union(arm64):
            self.__platform = "windows-amd64"
        else:
            raise UnsupportedPlatformError(
                f"Solidity compiler binaries are not available for {system}-{machine}."
            )

        self.__compilers_path = wake_config.global_data_path / "compilers"
        self.__compilers_path.mkdir(parents=True, exist_ok=True)

        if self.__platform == "linux-aarch64":
            # The official solc-bin repository publishes linux-arm64 binaries only
            # since Solidity 0.8.31. Use it as the primary source and fall back to
            # the 3rd-party nikitastupin mirror for older versions it does not
            # provide. Versions available from both are served by the official one.
            self.__sources = [
                _SolcSource(
                    list_urls=[
                        f"{self.BINARIES_URL}/linux-arm64/list.json",
                        f"{self.ARGOTORG_GITHUB_URL}/linux-arm64/list.json",
                    ],
                    download_urls=[
                        f"{self.BINARIES_URL}/linux-arm64/",
                        f"{self.ARGOTORG_GITHUB_URL}/linux-arm64/",
                    ],
                    cache_path=self.__compilers_path / "solc-linux-arm64.json",
                    third_party=False,
                ),
                _SolcSource(
                    list_urls=[
                        f"{self.NIKITA_LINUX_AARCH64_URL}/linux/aarch64/list.json",
                    ],
                    download_urls=[
                        f"{self.NIKITA_LINUX_AARCH64_URL}/linux/aarch64/",
                    ],
                    cache_path=self.__compilers_path / "solc-linux-aarch64-nikita.json",
                    third_party=True,
                ),
            ]
        else:
            self.__sources = [
                _SolcSource(
                    list_urls=[
                        f"{self.BINARIES_URL}/{self.__platform}/list.json",
                        f"{self.GITHUB_URL}/{self.__platform}/list.json",
                    ],
                    download_urls=[
                        f"{self.BINARIES_URL}/{self.__platform}/",
                        f"{self.GITHUB_URL}/{self.__platform}/",
                    ],
                    cache_path=self.__compilers_path / "solc.json",
                    third_party=False,
                ),
            ]

        self.__solc_builds = None
        self.__version_source = {}
        self.__list_force_loaded = False
        self.__headers = {
            "User-Agent": f"wake/{get_package_version('eth-wake')}",
        }

    @property
    def using_3rd_party_source(self) -> bool:
        """
        Whether a 3rd-party source is configured for the current platform. Note that
        on such platforms only some (older) versions are actually served by the
        3rd-party source; use [is_third_party][wake.svm.svm.SolcVersionManager.is_third_party]
        for a per-version check.
        """
        return any(source.third_party for source in self.__sources)

    def is_third_party(self, version: Union[SolidityVersion, str]) -> bool:
        """
        Whether the given version would be downloaded from a 3rd-party source.
        """
        if isinstance(version, str):
            version = SolidityVersion.fromstring(version)

        self.__fetch_list_file(version, force=False)
        source = self.__version_source.get(version)
        return source.third_party if source is not None else False

    def __all_list_urls(self) -> List[str]:
        return [url for source in self.__sources for url in source.list_urls]

    def installed(self, version: Union[SolidityVersion, str]) -> bool:
        if isinstance(version, str):
            version = SolidityVersion.fromstring(version)

        self.__fetch_list_file(version, force=False)
        if self.__solc_builds is None:
            raise RuntimeError(
                f"Unable to fetch or correctly parse solc list from {self.__all_list_urls()}."
            )

        path = self.get_path(version)
        if not path.is_file():
            return False

        if not self.__verify_checksums(version):
            return False
        return True

    async def install(
        self,
        version: Union[SolidityVersion, str],
        force_reinstall: bool = False,
        http_session: Optional[aiohttp.ClientSession] = None,
        progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ) -> None:
        if isinstance(version, str):
            version = SolidityVersion.fromstring(version)

        self.__fetch_list_file(version, force=False)
        if self.__solc_builds is None:
            raise RuntimeError(
                f"Unable to fetch or correctly parse solc list from {self.__all_list_urls()}."
            )

        minimal_version = self.list_all(force=False)[0]
        if version < minimal_version:
            raise UnsupportedVersionError(
                f"The minimal supported solc version for the current platform is `{minimal_version}`."
            )

        if version not in self.__solc_builds.releases:
            raise ValueError(f"solc version `{version}` does not exist.")

        filename = self.__solc_builds.releases[version]

        if self.get_path(version).is_file() and not force_reinstall:
            # cannot verify checksum for unzipped binaries
            if filename.endswith(".zip"):
                return
            # checksum verification passed
            if self.__verify_checksums(version):
                return

        source = self.__version_source[version]

        if source.third_party:
            logger.warning(
                "Using 3rd party source for solc binaries: https://github.com/nikitastupin/solc"
            )

        local_path = self.get_path(version).parent / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)

        for retry in range(self.INSTALL_RETRY_COUNT):
            download_url = (
                source.download_urls[retry % len(source.download_urls)] + filename
            )

            logger.debug(f"Downloading solc {version} from {download_url}")

            if http_session is None:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=600)
                ) as session:
                    await self.__download_file(
                        download_url, local_path, session, progress
                    )
            else:
                await self.__download_file(
                    download_url, local_path, http_session, progress
                )

            if self.__verify_checksums(version):
                break
            elif retry == self.INSTALL_RETRY_COUNT - 1:
                local_path.unlink()
                raise ChecksumError(
                    f"Checksum of the downloaded solc version `{version}` does not match the expected value."
                )

        # unzip older Windows solc binary zipped together with DLLs
        if filename.endswith(".zip"):
            local_path = self.__unzip(local_path)

        local_path.chmod(0o775)

    def remove(self, version: Union[SolidityVersion, str]) -> None:
        path = self.get_path(version).parent
        if path.is_dir():
            shutil.rmtree(path)
        else:
            raise ValueError(
                f"solc version `{version}` was not installed - cannot remove."
            )

    def get_path(self, version: Union[SolidityVersion, str]) -> Path:
        if isinstance(version, str):
            version = SolidityVersion.fromstring(version)

        self.__fetch_list_file(version, force=False)
        if self.__solc_builds is None:
            raise RuntimeError(
                f"Unable to fetch or correctly parse solc list from {self.__all_list_urls()}."
            )

        minimal_version = self.list_all(force=False)[0]
        if version < minimal_version:
            raise UnsupportedVersionError(
                f"The minimal supported solc version for the current platform is `{minimal_version}`."
            )

        if version not in self.__solc_builds.releases:
            raise ValueError(f"solc version `{version}` does not exist")

        filename = self.__solc_builds.releases[version]
        dirname = filename
        if dirname.endswith((".exe", ".zip")):
            dirname = dirname[:-4]
        if filename.endswith(".zip"):
            filename = filename[:-3] + "exe"
        return self.__compilers_path / dirname / filename

    def list_all(self, force: bool) -> Tuple[SolidityVersion, ...]:
        self.__fetch_list_file(None, force)
        if self.__solc_builds is None:
            raise RuntimeError(
                f"Unable to fetch or correctly parse solc list from {self.__all_list_urls()}."
            )

        return tuple(sorted(self.__solc_builds.releases.keys()))

    async def __download_file(
        self,
        url: str,
        path: Path,
        http_session: aiohttp.ClientSession,
        progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ) -> None:
        async with http_session.get(url, headers=self.__headers) as r:
            total_size = r.headers.get("Content-Length")
            if total_size is not None:
                total_size = int(total_size)
            downloaded_size = 0
            with path.open("wb") as f:
                async for chunk in r.content.iter_chunked(8 * 1024):
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    if total_size is not None and progress is not None:
                        await progress(downloaded_size, total_size)

    def __unzip(self, zip_path: Path) -> Path:
        """
        Unzip the Windows `solc` executable zip containing:
        - solc.exe - extract this file and rename it as `solc-windows-amd64-v{version}+commit.{commit}.exe`
        - soltest.exe - ignore this file (i.e. do not extract it)
        - extract any additional files (DLLs) next to the solc binary
        After that, delete the zip file.
        """
        base_path = zip_path.parent
        solc_filename = zip_path.name[:-3] + "exe"
        solc_path = zip_path.parent / solc_filename

        with ZipFile(zip_path, "r") as _zip:
            members = _zip.namelist()
            for member in members:
                if member == "soltest.exe":
                    # do not extract soltest.exe to save up the space
                    continue
                elif member == "solc.exe":
                    # rename solc.exe to the long name (containing version number, commit number etc.)
                    _zip.extract(member, base_path)
                    (base_path / "solc.exe").rename(solc_path)
                else:
                    # extract all the remaining files
                    _zip.extract(member, base_path)
        zip_path.unlink()
        return solc_path

    def __rebuild_merged(self) -> None:
        """
        Rebuild the merged view (``self.__solc_builds`` and ``self.__version_source``)
        from the per-source build lists. Earlier (higher-priority) sources take
        precedence for versions available from more than one source, so both the
        release filename and the checksum entry of an overlapping version come from
        the same source.
        """
        releases: Dict[SolidityVersion, str] = {}
        builds_by_key: Dict[Union[str, SolidityVersion], SolcBuildInfo] = {}
        version_source: Dict[SolidityVersion, _SolcSource] = {}
        latest_release: Optional[str] = None

        for source in self.__sources:
            if source.builds is None:
                continue
            if latest_release is None:
                latest_release = source.builds.latest_release
            for version, filename in source.builds.releases.items():
                if version not in releases:
                    releases[version] = filename
                    version_source[version] = source
            for build_info in source.builds.builds:
                # key by release filename so multiple builds sharing one version
                # (e.g. a release and its prereleases) are all retained; fall back
                # to version for sources whose entries omit the path (nikitastupin)
                key = (
                    build_info.path
                    if build_info.path is not None
                    else build_info.version
                )
                if key not in builds_by_key:
                    builds_by_key[key] = build_info

        if len(releases) == 0:
            self.__solc_builds = None
            self.__version_source = {}
            return

        self.__solc_builds = SolcBuilds(
            builds=list(builds_by_key.values()),
            releases=releases,
            latestRelease=latest_release,
        )
        self.__version_source = version_source

    def __fetch_source_list_from_url(self, source: _SolcSource, url: str) -> None:
        logger.debug(f"Downloading solc list from {url}")
        request = urllib.request.Request(url, headers=self.__headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            json = response.read()
            # validate before caching so a malformed response is never persisted
            source.builds = SolcBuilds.model_validate_json(json)
            source.cache_path.write_bytes(json)

    def __fetch_list_file(
        self, target_version: Optional[SolidityVersion], force: bool
    ) -> None:
        """
        Populate ``self.__solc_builds`` with a merged view over all configured
        sources for the current platform, caching each source's ``list.json`` under
        ``{global_data_path}/compilers/``. Network issues gracefully fall back to the
        locally cached lists.
        """
        # load any not-yet-loaded source from its on-disk cache (offline, cheap)
        for source in self.__sources:
            if source.builds is None and source.cache_path.is_file():
                try:
                    source.builds = SolcBuilds.model_validate_json(
                        source.cache_path.read_text()
                    )
                except ValidationError:
                    pass
        self.__rebuild_merged()

        # is what we already have sufficient?
        if self.__solc_builds is not None:
            if (
                target_version is not None
                and target_version in self.__solc_builds.releases
            ):
                return
            if self.__list_force_loaded:
                return
            if not force and all(
                source.builds is not None for source in self.__sources
            ):
                return

        # refresh every source from the network (primary first); each source tries
        # its mirror URLs in order and caches the first successful response
        fetched_any = False
        for source in self.__sources:
            for url in source.list_urls:
                try:
                    self.__fetch_source_list_from_url(source, url)
                    fetched_any = True
                    break
                except (urllib.error.URLError, OSError, ValidationError) as e:
                    logger.warning(f"Failed to download solc list from {url}: {e}")

        if fetched_any:
            self.__list_force_loaded = True
        self.__rebuild_merged()

        # no network and no usable cache anywhere
        if self.__solc_builds is None:
            raise RuntimeError(
                f"Unable to fetch or correctly parse solc list from {self.__all_list_urls()}."
            )

    def __verify_checksums(self, version: SolidityVersion) -> bool:
        assert self.__solc_builds is not None
        filename = self.__solc_builds.releases[version]
        # match the build entry by its release filename: a single version may have
        # multiple build entries (e.g. a release and its prereleases). Fall back to
        # matching by version for sources whose entries omit the path (nikitastupin).
        build_info = next(
            (b for b in self.__solc_builds.builds if b.path == filename), None
        )
        if build_info is None:
            build_info = next(
                b for b in self.__solc_builds.builds if b.version == version
            )
        local_path = self.get_path(version)

        if filename.endswith(".zip"):
            local_path = local_path.parent / filename
            if not local_path.is_file():
                return True

        sha256 = build_info.sha256
        if sha256.startswith("0x"):
            sha256 = sha256[2:]

        if not self.__verify_sha256(local_path, sha256):
            return False

        keccak256 = build_info.keccak256
        if keccak256 is not None:
            if keccak256.startswith("0x"):
                keccak256 = keccak256[2:]
            if not self.__verify_keccak256(local_path, keccak256):
                return False

        return True

    def __verify_sha256(self, path: Path, expected: str) -> bool:
        """
        Check SHA256 checksum of the provided file against the expected value.
        :param path: path of the file whose checksum to be verified
        :param expected: expected value of SHA256 checksum
        :return: True if checksum matches the expected value, False otherwise
        """
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(4 * 1024)
                if not chunk:
                    break
                h.update(chunk)

        return h.hexdigest() == expected

    def __verify_keccak256(self, path: Path, expected: str) -> bool:
        """
        Check KECCAK256 checksum of the provided file against the expected value.
        :param path: path of the file whose checksum to be verified
        :param expected: expected value of KECCAK256 checksum
        :return: True if checksum matches the expected value, False otherwise
        """
        h = keccak.new(digest_bits=256)
        with path.open("rb") as f:
            while True:
                chunk = f.read(4 * 1024)
                if not chunk:
                    break
                h.update(chunk)

        return h.hexdigest() == expected
