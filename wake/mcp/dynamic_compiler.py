import asyncio
import glob
import logging
import time
from pathlib import Path
from typing import Optional, Set

from watchdog.events import (
    FileClosedEvent,
    FileOpenedEvent,
    FileSystemEvent,
    FileSystemMovedEvent,
)
from watchdog.observers import Observer

from wake.compiler.compiler import (
    CompilationFileSystemEventHandler,
    ProjectBuild,
    ProjectBuildInfo,
    SolcOutputSelectionEnum,
    SolidityCompiler,
)
from wake.config import WakeConfig
from wake.utils import is_relative_to

from .common import McpBuild


class CompilationFileSystemEventHandlerWithEvents(CompilationFileSystemEventHandler):
    _queue_size: int
    _ready: asyncio.Event
    _ignored_paths: list[Path]

    def __init__(
        self, ready: asyncio.Event, ignored_paths: list[Path], *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self._queue_size = 0
        self._ready = ready
        self._ignored_paths = ignored_paths

    def on_any_event(self, event: FileSystemEvent):
        if event.is_directory:
            return
        if isinstance(event, FileSystemMovedEvent):
            src_file = Path(event.src_path)
            dest_file = Path(event.dest_path)

            if any(is_relative_to(src_file, p) for p in self._ignored_paths) and any(
                is_relative_to(dest_file, p) for p in self._ignored_paths
            ):
                return

            if (
                src_file == self._config.local_config_path
                or src_file.suffix == ".sol"
                or dest_file == self._config.local_config_path
                or dest_file.suffix == ".sol"
            ):
                self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
                self._queue_size += 1
                self._ready.clear()
        elif isinstance(event, (FileOpenedEvent, FileClosedEvent)):
            # ignore
            pass
        else:
            file = Path(event.src_path)
            if any(is_relative_to(file, p) for p in self._ignored_paths):
                return

            if file == self._config.local_config_path or file.suffix == ".sol":
                self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
                self._queue_size += 1
                self._ready.clear()

    async def run(self):
        while True:
            # process at least one event
            event = await self._queue.get()
            self._process_event(event)
            self._queue_size -= 1

            start = time.perf_counter()
            while time.perf_counter() - start < self.TIMEOUT_INTERVAL:
                try:
                    event = self._queue.get_nowait()
                    self._process_event(event)
                    self._queue_size -= 1
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.1)

            await self._compile()

            assert self._compiler.latest_build is not None
            assert self._compiler.latest_build_info is not None

            for callback in self._callbacks:
                callback(self._compiler.latest_build, self._compiler.latest_build_info)

            if self._queue_size == 0:
                self._ready.set()


# watches for changes to *.sol files and recompiles the project
class DynamicCompiler:
    _ready: asyncio.Event
    _execution_root: Path
    _logger: logging.Logger
    _local_config_path: Optional[Path]
    _latest_build: ProjectBuild
    _config: WakeConfig

    def __init__(
        self,
        execution_root: Path,
        logger: logging.Logger,
        local_config_path: Optional[Path] = None,
    ):
        self._ready = asyncio.Event()
        self._execution_root = execution_root
        self._logger = logger
        self._local_config_path = local_config_path

    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def get_config(self) -> WakeConfig:
        await self._ready.wait()
        return self._config

    async def get_build(self) -> McpBuild:
        await self._ready.wait()
        return McpBuild(
            project_root=self._config.project_root_path,
            relative_compilation_root=None,
            source_units=self._latest_build.source_units,
            interval_trees=self._latest_build.interval_trees,
            reference_resolver=self._latest_build.reference_resolver,
        )

    def _compilation_callback(self, build: ProjectBuild, info: ProjectBuildInfo):
        self._latest_build = build

    async def run(self, *, ignored_paths: Optional[list[Path]] = None):
        if ignored_paths is None:
            ignored_paths = []

        config = WakeConfig(local_config_path=self._local_config_path)
        config.load_configs()
        self._config = config

        compiler = SolidityCompiler(config)

        sol_files: Set[Path] = set()
        for f in glob.iglob(str(config.project_root_path / "**/*.sol"), recursive=True):
            file = Path(f)
            if (
                not any(
                    is_relative_to(file, p) for p in config.compiler.solc.exclude_paths
                )
                and not any(is_relative_to(file, p) for p in ignored_paths)
                and file.is_file()
            ):
                sol_files.add(file)

        fs_handler = CompilationFileSystemEventHandlerWithEvents(
            self._ready,
            ignored_paths,
            config,
            sol_files,
            asyncio.get_event_loop(),
            compiler,
            [SolcOutputSelectionEnum.ALL],
            write_artifacts=False,
        )
        fs_handler.register_callback(self._compilation_callback)
        observer = Observer()
        observer.schedule(fs_handler, str(Path.cwd()), recursive=True)
        observer.start()

        compiler.load()

        build, _ = await compiler.compile(
            sol_files,
            [SolcOutputSelectionEnum.ALL],
            write_artifacts=False,
        )
        assert compiler.latest_build_info is not None
        self._compilation_callback(build, compiler.latest_build_info)

        if fs_handler._queue_size == 0:
            self._ready.set()

        try:
            await fs_handler.run()
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            observer.join()
