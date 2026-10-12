from __future__ import annotations
from typing import Dict, Iterable, Iterator, List, Tuple, Union
import logging
import os
import stat
import subprocess

from ..SAOLogging import logging_fatal, perror

# Conservative upper bound for one adb transfer command line (adb binary,
# flags, verb and all path arguments combined). Well below the Windows
# CreateProcess limit (~32k characters) and any Unix ARG_MAX, so multi-source
# batches chunked to this budget are safe everywhere adbsync runs.
MAX_COMMAND_BYTES = 16000

# File leaves of the file trees are (atime, mtime, size) tuples with
# minute-truncated times; directory metadata (the "." entries) stays an
# (atime, mtime) 2-tuple. All tuple consumers must therefore tolerate both
# lengths (see tree_file_times / tree_file_size).

def tree_file_times(tree: tuple) -> Tuple[int, int]:
    """(atime, mtime) of a file-leaf or directory-metadata tuple."""
    return tree[0], tree[1]

def tree_file_size(tree: tuple) -> Union[int, None]:
    """Size of a file-leaf tuple, or None for legacy 2-tuples / directories."""
    return tree[2] if len(tree) > 2 else None

def _encoded_len(argument: str) -> int:
    return len(argument.encode("utf-8", "surrogateescape")) + 1  # +1 for the separating space/NUL

def chunk_arguments(arguments: List[str], overhead_bytes: int = 0, max_bytes: int = MAX_COMMAND_BYTES) -> Iterator[List[str]]:
    """Yield consecutive sub-lists of arguments that fit the command-byte budget."""
    chunk: List[str] = []
    chunk_bytes = overhead_bytes
    for argument in arguments:
        argument_bytes = _encoded_len(argument)
        if chunk and chunk_bytes + argument_bytes > max_bytes:
            yield chunk
            chunk = []
            chunk_bytes = overhead_bytes
        chunk.append(argument)
        chunk_bytes += argument_bytes
    if chunk:
        yield chunk


class FileSystem:
    def __init__(self, adb_arguments: List[str], skip_permission_denied: bool) -> None:
        self.adb_arguments = adb_arguments
        self.skip_permission_denied = skip_permission_denied
        self.permission_denied_skipped = []

    def _command_overhead_bytes(self, extra_arguments: List[str]) -> int:
        """Bytes already spent on the adb binary, its flags and fixed arguments."""
        return sum(_encoded_len(arg) for arg in self.adb_arguments) \
            + sum(_encoded_len(arg) for arg in extra_arguments) + 64  # slack

    def _get_files_tree(self, tree_path: str, tree_path_stat: os.stat_result, follow_links: bool = False):
        # the reason to have two functions instead of one purely recursive one is to use self.lstat_in_dir ie ls
        # which is much faster than individually stat-ing each file. Hence, we have get_files_tree's special first lstat
        if stat.S_ISLNK(tree_path_stat.st_mode):
            if not follow_links:
                logging.warning(f"Ignoring symlink {tree_path}")
                return None
            logging.debug(f"Following symlink {tree_path}")
            try:
                tree_path_realpath = self.realpath(tree_path)
                tree_path_stat_realpath = self.lstat(tree_path_realpath)
            except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
                perror(f"Skipping symlink {tree_path}", e)
                return None
            return self._get_files_tree(tree_path_realpath, tree_path_stat_realpath, follow_links = follow_links)
        elif stat.S_ISDIR(tree_path_stat.st_mode):
            tree = {".": (60 * (int(tree_path_stat.st_atime) // 60), 60 * (int(tree_path_stat.st_mtime) // 60))}
            try:
                for filename, stat_object_child, in self.lstat_in_dir(tree_path):
                    if filename in [".", ".."]:
                        continue
                    tree[filename] = self._get_files_tree(
                        self.join(tree_path, filename),
                        stat_object_child,
                        follow_links = follow_links)
                return tree
            except PermissionError as e:
                if self.skip_permission_denied:
                    self.permission_denied_skipped.append(e.filename)
                    logging.warning(f"Skipping sync of directory {e.filename} - access denied")
                    return None
                else:
                    raise e
        elif stat.S_ISREG(tree_path_stat.st_mode):
            return (
                60 * (int(tree_path_stat.st_atime) // 60),
                60 * (int(tree_path_stat.st_mtime) // 60), # minute resolution
                tree_path_stat.st_size,
            )
        else:
            raise NotImplementedError

    def get_files_tree(self, tree_path: str, follow_links: bool = False):
        stat_object = self.lstat(tree_path)
        return self._get_files_tree(tree_path, stat_object, follow_links = follow_links)

    def remove_tree(self, tree_path: str, tree: Union[Tuple[int, int], dict], dry_run: bool = True) -> None:
        if isinstance(tree, tuple):
            logging.info(f"Removing {tree_path}")
            if not dry_run:
                self.unlink(tree_path)
        elif isinstance(tree, dict):
            remove_folder = tree.pop(".", False)
            for key, value in tree.items():
                self.remove_tree(self.normpath(self.join(tree_path, key)), value, dry_run = dry_run)
            if remove_folder:
                logging.info(f"Removing folder {tree_path}")
                if not dry_run:
                    self.rmdir(tree_path)
        else:
            raise NotImplementedError

    def push_tree_here(self,
        tree_path: str,
        relative_tree_path: str, # for logging paths of files / folders copied relative to the source root / destination root
                                 # nicely instead of repeating the root every time; rsync does this nice logging
        tree: Union[Tuple[int, int], dict],
        destination_root: str,
        fs_source: FileSystem,
        dry_run: bool = True,
        show_progress: bool = False
        ) -> Tuple[int, int]:
        """Classic per-file copy executor. Returns (files copied, bytes copied)."""
        if isinstance(tree, tuple):
            if dry_run:
                logging.info(f"{relative_tree_path}")
                return 0, 0
            else:
                logging.debug("%s", relative_tree_path)
                self.push_file_here(tree_path, destination_root, show_progress = show_progress)
                self.utime(destination_root, tree_file_times(tree))
                return 1, tree_file_size(tree) or 0
        elif isinstance(tree, dict):
            files_copied = 0
            bytes_copied = 0
            try:
                tree.pop(".") # directory needs making
                if dry_run:
                    logging.info(f"{relative_tree_path}{self.sep}")
                else:
                    logging.debug(f"{relative_tree_path}{self.sep}")
                    self.makedirs(destination_root)
            except KeyError:
                pass
            for key, value in tree.items():
                child_files, child_bytes = self.push_tree_here(
                    fs_source.normpath(fs_source.join(tree_path, key)),
                    fs_source.join(relative_tree_path, key),
                    value,
                    self.normpath(self.join(destination_root, key)),
                    fs_source,
                    dry_run = dry_run,
                    show_progress = show_progress
                )
                files_copied += child_files
                bytes_copied += child_bytes
            return files_copied, bytes_copied
        else:
            raise NotImplementedError

    def push_tree_here_batched(self,
        tree_path: str,
        tree: dict,
        destination_root: str,
        fs_source: FileSystem,
        show_progress: bool = False
        ) -> Tuple[int, int]:
        """Batched copy executor: one adb process per (destination parent, chunk).

        Produces exactly the same destination layout as push_tree_here, but
        groups files by destination parent directory and transfers each group
        with adb's multi-source form. Directory creation and timestamp
        restoration are batched as well. Returns (files copied, bytes copied).
        """
        directories: List[str] = []
        files: List[Tuple[str, str, int, int, int]] = []  # source, destination, atime, mtime, size
        self._flatten_copy_tree(tree_path, tree, destination_root, fs_source, directories, files)

        if directories:
            self.makedirs_many(directories)

        files_by_destination_parent: Dict[str, List[str]] = {}
        for file_source, file_destination, _, _, _ in files:
            destination_parent = self.normpath(self.split(file_destination)[0])
            files_by_destination_parent.setdefault(destination_parent, []).append(file_source)

        for destination_parent, sources in files_by_destination_parent.items():
            logging.debug("Copying %d file(s) into %s", len(sources), destination_parent)
            self.push_files_here(sources, destination_parent, show_progress = show_progress)

        if files:
            self.utime_many([(destination, (atime, mtime)) for _, destination, atime, mtime, _ in files])

        return len(files), sum(size for _, _, _, _, size in files)

    def _flatten_copy_tree(self,
        tree_path: str,
        tree: Union[tuple, dict],
        destination_root: str,
        fs_source: FileSystem,
        directories: List[str],
        files: List[Tuple[str, str, int, int, int]]
        ) -> None:
        """Flatten a copy tree into directory-creations and file copies.

        Computes exactly the same source/destination path pairs as
        push_tree_here's recursion.
        """
        if isinstance(tree, tuple):
            atime, mtime = tree_file_times(tree)
            files.append((tree_path, destination_root, atime, mtime, tree_file_size(tree) or 0))
            logging.debug("%s --> %s", tree_path, destination_root)
        elif isinstance(tree, dict):
            if "." in tree:
                tree.pop(".") # directory needs making
                directories.append(destination_root)
            for key, value in tree.items():
                self._flatten_copy_tree(
                    fs_source.normpath(fs_source.join(tree_path, key)),
                    value,
                    self.normpath(self.join(destination_root, key)),
                    fs_source,
                    directories,
                    files
                )
        else:
            raise NotImplementedError

    # Checked transfer primitive shared by the per-file and batched executors.

    @staticmethod
    def _run_transfer_command(adb_cmd: List[str], verb: str, subject: str, show_progress: bool = False) -> None:
        """Run one adb push/pull and turn a non-zero exit into a fatal error.

        `subject` identifies direction and path(s) for the diagnostic message.
        A failed transfer must never be treated as successful (in particular
        no destination timestamps are applied afterwards by the caller).
        """
        if show_progress:
            # Let adb's own progress/error output through to the user.
            result = subprocess.run(adb_cmd)
            stderr_tail = ""
        else:
            result = subprocess.run(adb_cmd, stdout = subprocess.DEVNULL, stderr = subprocess.PIPE)
            stderr_tail = result.stderr.decode("utf-8", errors = "replace").strip()
        if result.returncode != 0:
            diagnostics = f" (exit code {result.returncode})"
            if stderr_tail:
                diagnostics += f"; stderr: {stderr_tail[-500:]}"
            logging_fatal(f"Non-zero exit code from adb {verb} for {subject}{diagnostics}")

    # Abstract methods below implemented in Local.py and Android.py

    @property
    def sep(self) -> str:
        raise NotImplementedError

    def unlink(self, path: str) -> None:
        raise NotImplementedError

    def rmdir(self, path: str) -> None:
        raise NotImplementedError

    def makedirs(self, path: str) -> None:
        raise NotImplementedError

    def makedirs_many(self, paths: List[str]) -> None:
        raise NotImplementedError

    def realpath(self, path: str) -> str:
        raise NotImplementedError

    def lstat(self, path: str) -> os.stat_result:
        raise NotImplementedError

    def lstat_in_dir(self, path: str) -> Iterable[Tuple[str, os.stat_result]]:
        raise NotImplementedError

    def utime(self, path: str, times: Tuple[int, int]) -> None:
        raise NotImplementedError

    def utime_many(self, entries: List[Tuple[str, Tuple[int, int]]]) -> None:
        raise NotImplementedError

    def join(self, base: str, leaf: str) -> str:
        raise NotImplementedError

    def split(self, path: str) -> Tuple[str, str]:
        raise NotImplementedError

    def normpath(self, path: str) -> str:
        raise NotImplementedError

    def push_file_here(self, source: str, destination: str, show_progress: bool = False) -> None:
        raise NotImplementedError

    def push_files_here(self, sources: List[str], destination_dir: str, show_progress: bool = False) -> None:
        """Multi-source batched transfer; destination_dir must already exist."""
        raise NotImplementedError

    def push_recursive_here(self, source: str, destination: str, show_progress: bool = False) -> None:
        """One recursive transfer of a whole directory tree to a missing destination."""
        raise NotImplementedError
