from typing import Iterable, List, Tuple
import os

from .Base import FileSystem, chunk_arguments

class LocalFileSystem(FileSystem):
    @property
    def sep(self) -> str:
        return os.path.sep

    def unlink(self, path: str) -> None:
        os.unlink(path)

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok = True)

    def makedirs_many(self, paths: List[str]) -> None:
        for path in paths:
            os.makedirs(path, exist_ok = True)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def lstat_in_dir(self, path: str) -> Iterable[Tuple[str, os.stat_result]]:
        for filename in os.listdir(path):
            yield filename, self.lstat(self.join(path, filename))

    def utime(self, path: str, times: Tuple[int, int]) -> None:
        os.utime(path, times=times)

    def utime_many(self, entries: List[Tuple[str, Tuple[int, int]]]) -> None:
        for path, times in entries:
            os.utime(path, times=times)

    def join(self, base: str, leaf: str) -> str:
        return os.path.join(base, leaf)

    def split(self, path: str) -> Tuple[str, str]:
        return os.path.split(path)

    def normpath(self, path: str) -> str:
        return os.path.normpath(path)

    def push_file_here(self, source: str, destination: str, show_progress: bool = False) -> None:
        self._run_transfer_command(
            self.adb_arguments + ["pull", source, destination],
            "pull",
            f"{source} -> {destination}",
            show_progress = show_progress
        )

    def push_files_here(self, sources: List[str], destination_dir: str, show_progress: bool = False) -> None:
        overhead = self._command_overhead_bytes(["pull", destination_dir])
        for chunk in chunk_arguments(sources, overhead_bytes = overhead):
            self._run_transfer_command(
                self.adb_arguments + ["pull"] + chunk + [destination_dir],
                "pull",
                f"{len(chunk)} file(s) into {destination_dir} (first: {chunk[0]})",
                show_progress = show_progress
            )

    def push_recursive_here(self, source: str, destination: str, show_progress: bool = False) -> None:
        # -a preserves the source modification times, matching the timestamp
        # policy of the classic path without any post-copy utime pass.
        self._run_transfer_command(
            self.adb_arguments + ["pull", "-a", source, destination],
            "pull",
            f"recursive copy of {source} -> {destination}",
            show_progress = show_progress
        )
