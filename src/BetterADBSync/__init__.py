#!/usr/bin/env python3

"""Sync files between a computer and an Android device"""


import fnmatch
import importlib.metadata
import logging
import os
import stat
import time
from typing import List, Tuple, Union

from BetterADBSync import adb
from .FileSystems.Android import AndroidFileSystem
from .FileSystems.Base import FileSystem, tree_file_size
from .FileSystems.Local import LocalFileSystem
from .SAOLogging import logging_fatal, log_tree, setup_root_logger, perror, pfatal
from .argparsing import get_cli_args

__version__ = importlib.metadata.version(__name__)

class FileSyncer:
    @classmethod
    def diff_trees(cls,
        source: Union[dict, Tuple[int, int], None],
        destination: Union[dict, Tuple[int, int], None],
        path_source: str,
        path_destination: str,
        destination_exclude_patterns: List[str],
        path_join_function_source,
        path_join_function_destination,
        folder_file_overwrite_error: bool = True,
        ) -> Tuple[
            Union[dict, Tuple[int, int], None], # delete
            Union[dict, Tuple[int, int], None], # copy
            Union[dict, Tuple[int, int], None], # excluded_source
            Union[dict, Tuple[int, int], None], # unaccounted_destination
            Union[dict, Tuple[int, int], None]  # excluded_destination
        ]:

        exclude = False
        for destination_exclude_pattern in destination_exclude_patterns:
            if fnmatch.fnmatch(path_destination, destination_exclude_pattern):
                exclude = True
                break

        if source is None:
            if destination is None:
                delete = None
                copy = None
                excluded_source = None
                unaccounted_destination = None
                excluded_destination = None
            elif isinstance(destination, tuple):
                if exclude:
                    delete = None
                    copy = None
                    excluded_source = None
                    unaccounted_destination = None
                    excluded_destination = destination
                else:
                    delete = None
                    copy = None
                    excluded_source = None
                    unaccounted_destination = destination
                    excluded_destination = None
            elif isinstance(destination, dict):
                if exclude:
                    delete = {".": None}
                    copy = None
                    excluded_source = None
                    unaccounted_destination = {".": None}
                    excluded_destination = destination
                else:
                    delete = {".": None}
                    copy = None
                    excluded_source = None
                    unaccounted_destination = {".": destination["."]}
                    excluded_destination = {".": None}
                    destination.pop(".")
                    for key, value in destination.items():
                        delete[key], _, _, unaccounted_destination[key], excluded_destination[key] = cls.diff_trees(
                            None,
                            value,
                            path_join_function_source(path_source, key),
                            path_join_function_destination(path_destination, key),
                            destination_exclude_patterns,
                            path_join_function_source,
                            path_join_function_destination,
                            folder_file_overwrite_error = folder_file_overwrite_error
                        )
            else:
                raise NotImplementedError

        elif isinstance(source, tuple):
            if destination is None:
                if exclude:
                    delete = None
                    copy = None
                    excluded_source = source
                    unaccounted_destination = None
                    excluded_destination = None
                else:
                    delete = None
                    copy = source
                    excluded_source = None
                    unaccounted_destination = None
                    excluded_destination = None
            elif isinstance(destination, tuple):
                if exclude:
                    delete = None
                    copy = None
                    excluded_source = source
                    unaccounted_destination = None
                    excluded_destination = destination
                else:
                    # Copy when the source is newer (existing conflict rule) or
                    # when sizes differ; the latter catches same-minute content
                    # changes and partial destinations left by failed transfers.
                    source_size = tree_file_size(source)
                    destination_size = tree_file_size(destination)
                    if source[1] > destination[1] or (
                        source_size is not None
                        and destination_size is not None
                        and source_size != destination_size
                    ):
                        delete = destination
                        copy = source
                        excluded_source = None
                        unaccounted_destination = None
                        excluded_destination = None
                    else:
                        delete = None
                        copy = None
                        excluded_source = None
                        unaccounted_destination = None
                        excluded_destination = None
            elif isinstance(destination, dict):
                if exclude:
                    delete = {".": None}
                    copy = None
                    excluded_source = source
                    unaccounted_destination = {".": None}
                    excluded_destination = destination
                else:
                    delete = destination
                    copy = source
                    excluded_source = None
                    unaccounted_destination = {".": None}
                    excluded_destination = {".": None}
                    if folder_file_overwrite_error:
                        logging.critical(f"Refusing to overwrite directory {path_destination} with file {path_source}")
                        logging_fatal("Use --force if you are sure!")
                    else:
                        logging.warning(f"Overwriting directory {path_destination} with file {path_source}")
            else:
                raise NotImplementedError

        elif isinstance(source, dict):
            if destination is None:
                if exclude:
                    delete = None
                    copy = {".": None}
                    excluded_source = source
                    unaccounted_destination = None
                    excluded_destination = None
                else:
                    delete = None
                    copy = {".": source["."]}
                    excluded_source = {".": None}
                    unaccounted_destination = None
                    excluded_destination = None
                    source.pop(".")
                    for key, value in source.items():
                        _, copy[key], excluded_source[key], _, _ = cls.diff_trees(
                            value,
                            None,
                            path_join_function_source(path_source, key),
                            path_join_function_destination(path_destination, key),
                            destination_exclude_patterns,
                            path_join_function_source,
                            path_join_function_destination,
                            folder_file_overwrite_error = folder_file_overwrite_error
                        )
            elif isinstance(destination, tuple):
                if exclude:
                    delete = None
                    copy = {".": None}
                    excluded_source = source
                    unaccounted_destination = None
                    excluded_destination = destination
                else:
                    delete = destination
                    copy = {".": source["."]}
                    excluded_source = {".": None}
                    unaccounted_destination = None
                    excluded_destination = None
                    source.pop(".")
                    for key, value in source.items():
                        _, copy[key], excluded_source[key], _, _ = cls.diff_trees(
                            value,
                            None,
                            path_join_function_source(path_source, key),
                            path_join_function_destination(path_destination, key),
                            destination_exclude_patterns,
                            path_join_function_source,
                            path_join_function_destination,
                            folder_file_overwrite_error = folder_file_overwrite_error
                        )
                    if folder_file_overwrite_error:
                        logging.critical(f"Refusing to overwrite file {path_destination} with directory {path_source}")
                        logging_fatal("Use --force if you are sure!")
                    else:
                        logging.warning(f"Overwriting file {path_destination} with directory {path_source}")
                excluded_destination = None
            elif isinstance(destination, dict):
                if exclude:
                    delete = {".": None}
                    copy = {".": None}
                    excluded_source = source
                    unaccounted_destination = {".": None}
                    excluded_destination = destination
                else:
                    delete = {".": None}
                    copy = {".": None}
                    excluded_source = {".": None}
                    unaccounted_destination = {".": None}
                    excluded_destination = {".": None}
                    source.pop(".")
                    for key, value in source.items():
                        delete[key], copy[key], excluded_source[key], unaccounted_destination[key], excluded_destination[key] = cls.diff_trees(
                            value,
                            destination.pop(key, None),
                            path_join_function_source(path_source, key),
                            path_join_function_destination(path_destination, key),
                            destination_exclude_patterns,
                            path_join_function_source,
                            path_join_function_destination,
                            folder_file_overwrite_error = folder_file_overwrite_error
                        )
                    destination.pop(".")
                    for key, value in destination.items():
                        delete[key], _, _, unaccounted_destination[key], excluded_destination[key] = cls.diff_trees(
                            None,
                            value,
                            path_join_function_source(path_source, key),
                            path_join_function_destination(path_destination, key),
                            destination_exclude_patterns,
                            path_join_function_source,
                            path_join_function_destination,
                            folder_file_overwrite_error = folder_file_overwrite_error
                        )
            else:
                raise NotImplementedError

        else:
            raise NotImplementedError

        return delete, copy, excluded_source, unaccounted_destination, excluded_destination

    @classmethod
    def remove_excluded_folders_from_unaccounted_tree(cls, unaccounted: Union[dict, Tuple[int, int]], excluded: Union[dict, None]) -> dict:
        # For when we have --del but not --delete-excluded selected; we do not want to delete unaccounted folders that are the
        # parent of excluded items. At the point in the program that this function is called at either
        # 1) unaccounted is a tuple (file) and excluded is None
        # 2) unaccounted is a dict and excluded is a dict or None
        # trees passed to this function are already pruned; empty dictionary (sub)trees don't exist
        if excluded is None:
            return unaccounted
        else:
            unaccounted_non_excluded = {}
            for unaccounted_key, unaccounted_value in unaccounted.items():
                if unaccounted_key == ".":
                    continue
                unaccounted_non_excluded[unaccounted_key] = cls.remove_excluded_folders_from_unaccounted_tree(
                    unaccounted_value,
                    excluded.get(unaccounted_key, None)
                )
            return unaccounted_non_excluded

    @classmethod
    def prune_tree(cls, tree):
        """Remove all Nones from a tree. May return None if tree is None however."""
        if not isinstance(tree, dict):
            return tree
        else:
            return_dict = {}
            for key, value in tree.items():
                value_pruned = cls.prune_tree(value)
                if value_pruned is not None:
                    return_dict[key] = value_pruned
            return return_dict or None

    @classmethod
    def sort_tree(cls, tree):
        if not isinstance(tree, dict):
            return tree
        return {
            k: cls.sort_tree(v)
            for k, v in sorted(tree.items())
        }

    @classmethod
    def paths_to_fixed_destination_paths(cls,
        path_source: str,
        fs_source: FileSystem,
        path_destination: str,
        fs_destination: FileSystem
    ) -> Tuple[str, str]:
        """Modify sync paths according to how a trailing slash on the source path should be treated"""
        # TODO I'm not exactly sure if this covers source and destination being symlinks (lstat vs stat etc)
        # we only need to consider when the destination is a directory
        try:
            lstat_destination = fs_destination.lstat(path_destination)
        except FileNotFoundError:
            return path_source, path_destination
        except (NotADirectoryError, PermissionError) as e:
            pfatal(path_source, e)

        if stat.S_ISLNK(lstat_destination.st_mode):
            logging_fatal("Destination is a symlink. Not sure what to do. See GitHub issue #8")

        if not stat.S_ISDIR(lstat_destination.st_mode):
            return path_source, path_destination

        # we know the destination is a directory at this point
        try:
            lstat_source = fs_source.lstat(path_source)
        except FileNotFoundError:
            return path_source, path_destination
        except (NotADirectoryError, PermissionError) as e:
            pfatal(path_source, e)

        if stat.S_ISREG(lstat_source.st_mode) or (stat.S_ISDIR(lstat_source.st_mode) and path_source[-1] not in ["/", "\\"]):
            path_destination = fs_destination.join(
                path_destination,
                fs_destination.split(path_source)[1]
            )
        return path_source, path_destination

def _tree_file_stats(tree) -> Tuple[int, int]:
    """(file count, total bytes) over all file leaves of a tree."""
    if tree is None:
        return 0, 0
    if isinstance(tree, tuple):
        return 1, tree_file_size(tree) or 0
    files = 0
    total_bytes = 0
    for key, value in tree.items():
        if key == ".":
            continue
        child_files, child_bytes = _tree_file_stats(value)
        files += child_files
        total_bytes += child_bytes
    return files, total_bytes

def _default_transfer_options(args) -> bool:
    """True when no option requires the classic planner/executor.

    The fast paths (one recursive transfer, batched transfers) reproduce the
    default behavior exactly; any advanced option falls back to the classic
    code paths so its semantics stay byte-for-byte identical.
    """
    return not (
        args.dry_run
        or args.copy_links
        or args.exclude
        or args.exclude_from
        or args.delete
        or args.delete_excluded
        or args.force
        or args.skip_permission_denied
    )

def _walk_local_tree(root: str) -> Union[Tuple[int, int], None]:
    """(file count, total bytes) of a local tree, or None if it contains any symlink.

    The classic planner ignores symlinks (unless -L), so a tree containing
    symlinks is not eligible for the recursive fast path.
    """
    files = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks = False):
        for name in dirnames:
            if os.path.islink(os.path.join(dirpath, name)):
                return None
        for name in filenames:
            full_path = os.path.join(dirpath, name)
            if os.path.islink(full_path):
                return None
            files += 1
            try:
                total_bytes += os.lstat(full_path).st_size
            except OSError:
                return None
    return files, total_bytes

def _try_recursive_fast_path(args, fs_source: FileSystem, path_source: str,
    fs_destination: FileSystem, path_destination: str) -> Union[Tuple[int, int], None]:
    """Copy a whole directory tree with ONE recursive adb push/pull when eligible.

    Eligibility (all verified here): default options, source is a real
    directory (not a symlink), destination root is missing (already mapped by
    paths_to_fixed_destination_paths, so trailing-slash/layout semantics match
    the classic path), and for push the local source tree contains no
    symlinks. Returns (files copied, bytes copied) when the fast path ran,
    None to fall back to the classic planner.
    """
    try:
        lstat_source = fs_source.lstat(path_source)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    if not stat.S_ISDIR(lstat_source.st_mode) or stat.S_ISLNK(lstat_source.st_mode):
        return None
    try:
        fs_destination.lstat(path_destination)
        return None # destination exists: the classic planner must diff against it
    except FileNotFoundError:
        pass
    except (NotADirectoryError, PermissionError):
        return None

    if args.direction == "push":
        # Local walks are cheap and double as the summary's file/byte counts.
        walk_result = _walk_local_tree(path_source)
        if walk_result is None:
            return None
        files_copied, bytes_copied = walk_result
    fs_destination.push_recursive_here(path_source, path_destination, show_progress = args.show_progress)
    if args.direction == "pull":
        # Count what actually arrived (no remote inventory is needed).
        files_copied, bytes_copied = _walk_local_tree(path_destination) or (0, 0)
    logging.info(f"Copied {path_source} --> {path_destination} with one recursive adb {args.direction}")
    return files_copied, bytes_copied

def main():
    args = get_cli_args(__doc__, __version__)

    # --- SHELL COMPLETION DISPATCH (keep this block intact) -----------------
    # `adbsync completion <shell>` prints a completion script to stdout and
    # exits before any ADB/device interaction. The internal helper
    # `adbsync completion-remote-paths DIR` (hidden from --help) lists the
    # immediate children of a remote directory for the generated completion
    # scripts; it enforces its own short timeout so it can never hang a shell.
    if args.direction in ("completion", "completion-remote-paths"):
        from .completion import run_completion_command
        raise SystemExit(run_completion_command(args))
    # --- END SHELL COMPLETION DISPATCH --------------------------------------

    started = time.perf_counter()

    setup_root_logger(
        no_color = args.logging_no_color,
        verbosity_level = args.logging_verbosity_verbose,
        quietness_level = args.logging_verbosity_quiet,
        messagefmt = "[%(levelname)s] %(message)s" if os.name == "nt" else "%(message)s"
    )

    for exclude_from_pathname in args.exclude_from:
        with exclude_from_pathname.open("r") as f:
            args.exclude.extend(line for line in f.read().splitlines() if line)

    if args.adb_bin is None:
        adb_bin = adb.find_adb_executable()
    else:
        adb_bin = args.adb_bin
    adb_arguments = [adb_bin] + [f"-{arg}" for arg in args.adb_flags]
    for option, value in args.adb_options:
        adb_arguments.append(f"-{option}")
        adb_arguments.append(value)

    fs_android = AndroidFileSystem(adb_arguments, args.adb_encoding, args.skip_permission_denied)
    fs_local = LocalFileSystem(adb_arguments, args.skip_permission_denied)

    try:
        fs_android.test_connection()
    except BrokenPipeError:
        logging_fatal("Connection test failed")

    if args.direction == "push":
        path_source = args.direction_push_local
        fs_source = fs_local
        path_destination = args.direction_push_android
        fs_destination = fs_android
    else:
        path_source = args.direction_pull_android
        fs_source = fs_android
        path_destination = args.direction_pull_local
        fs_destination = fs_local

    path_source, path_destination = FileSyncer.paths_to_fixed_destination_paths(path_source, fs_source, path_destination, fs_destination)

    path_source = fs_source.normpath(path_source)
    path_destination = fs_destination.normpath(path_destination)

    # Fast path: a directory source and a brand-new destination can be copied
    # with a single recursive adb push/pull, skipping both full-tree builds.
    if _default_transfer_options(args):
        fast_result = _try_recursive_fast_path(args, fs_source, path_source, fs_destination, path_destination)
        if fast_result is not None:
            files_copied, bytes_copied = fast_result
            elapsed = time.perf_counter() - started
            logging.info(f"Summary: copied {files_copied} files ({bytes_copied} bytes); "
                         f"0 skipped; 0 failed; elapsed {elapsed:.2f}s")
            return

    try:
        files_tree_source = fs_source.get_files_tree(path_source, follow_links = args.copy_links)
    except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
        pfatal(path_source, e)

    total_source_files, _ = _tree_file_stats(files_tree_source)

    try:
        files_tree_destination = fs_destination.get_files_tree(path_destination, follow_links = args.copy_links)
    except FileNotFoundError:
        files_tree_destination = None
    except (NotADirectoryError, PermissionError) as e:
        pfatal(path_destination, e)

    # Full tree dumps and per-file operation lines are part of the plan
    # display: shown for dry runs and verbose runs only, never by default.
    plan_log_level = logging.INFO if (args.dry_run or args.logging_verbosity_verbose > 0) else logging.DEBUG

    logging.log(plan_log_level, "Source tree:")
    if files_tree_source is not None:
        log_tree(path_source, files_tree_source, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    logging.log(plan_log_level, "Destination tree:")
    if files_tree_destination is not None:
        log_tree(path_destination, files_tree_destination, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    if isinstance(files_tree_source, dict):
        exclude_patterns = [fs_destination.normpath(
            fs_destination.join(path_destination, exclude)
        ) for exclude in args.exclude]
    else:
        exclude_patterns = [fs_destination.normpath(
            path_destination + exclude
        ) for exclude in args.exclude]
    logging.debug("Exclude patterns:")
    logging.debug(exclude_patterns)
    logging.debug("")

    tree_delete, tree_copy, tree_excluded_source, tree_unaccounted_destination, tree_excluded_destination = FileSyncer.diff_trees(
        files_tree_source,
        files_tree_destination,
        path_source,
        path_destination,
        exclude_patterns,
        fs_source.join,
        fs_destination.join,
        folder_file_overwrite_error = not args.dry_run and not args.force
    )

    tree_delete                  = FileSyncer.prune_tree(tree_delete)
    tree_copy                    = FileSyncer.prune_tree(tree_copy)
    tree_excluded_source         = FileSyncer.prune_tree(tree_excluded_source)
    tree_unaccounted_destination = FileSyncer.prune_tree(tree_unaccounted_destination)
    tree_excluded_destination    = FileSyncer.prune_tree(tree_excluded_destination)

    if plan_log_level == logging.INFO:
        # Sorting whole trees is only worth its cost when the plan is displayed.
        tree_delete                  = FileSyncer.sort_tree(tree_delete)
        tree_copy                    = FileSyncer.sort_tree(tree_copy)
        tree_excluded_source         = FileSyncer.sort_tree(tree_excluded_source)
        tree_unaccounted_destination = FileSyncer.sort_tree(tree_unaccounted_destination)
        tree_excluded_destination    = FileSyncer.sort_tree(tree_excluded_destination)

    logging.log(plan_log_level, "Delete tree:")
    if tree_delete is not None:
        log_tree(path_destination, tree_delete, log_leaves_types = False, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    logging.log(plan_log_level, "Copy tree:")
    if tree_copy is not None:
        log_tree(f"{path_source} --> {path_destination}", tree_copy, log_leaves_types = False, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    logging.log(plan_log_level, "Source excluded tree:")
    if tree_excluded_source is not None:
        log_tree(path_source, tree_excluded_source, log_leaves_types = False, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    logging.log(plan_log_level, "Destination unaccounted tree:")
    if tree_unaccounted_destination is not None:
        log_tree(path_destination, tree_unaccounted_destination, log_leaves_types = False, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    logging.log(plan_log_level, "Destination excluded tree:")
    if tree_excluded_destination is not None:
        log_tree(path_destination, tree_excluded_destination, log_leaves_types = False, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    if len(fs_source.permission_denied_skipped) > 0:
        logging.warning("Source elements skipped because of permission denied error:")
        for filename in fs_source.permission_denied_skipped:
            logging.warning(f"    {filename}")
        logging.info("")

    tree_unaccounted_destination_non_excluded = None
    if tree_unaccounted_destination is not None:
        tree_unaccounted_destination_non_excluded = FileSyncer.prune_tree(
            FileSyncer.remove_excluded_folders_from_unaccounted_tree(
                tree_unaccounted_destination,
                tree_excluded_destination
            )
        )

    logging.log(plan_log_level, "Non-excluded-supporting destination unaccounted tree:")
    if tree_unaccounted_destination_non_excluded is not None:
        log_tree(path_destination, tree_unaccounted_destination_non_excluded, log_leaves_types = False, logging_level = plan_log_level)
    logging.log(plan_log_level, "")

    logging.info("SYNCING")
    logging.info("")

    if tree_delete is not None:
        logging.info("Deleting delete tree")
        fs_destination.remove_tree(path_destination, tree_delete, dry_run = args.dry_run)
    else:
        logging.info("Empty delete tree")
    logging.info("")

    if args.delete_excluded and args.delete:
        if tree_excluded_destination is not None:
            logging.info("Deleting destination excluded tree")
            fs_destination.remove_tree(path_destination, tree_excluded_destination, dry_run = args.dry_run)
        else:
            logging.info("Empty destination excluded tree")
        logging.info("")
        if tree_unaccounted_destination is not None:
            logging.info("Deleting destination unaccounted tree")
            fs_destination.remove_tree(path_destination, tree_unaccounted_destination, dry_run = args.dry_run)
        else:
            logging.info("Empty destination unaccounted tree")
        logging.info("")
    elif args.delete_excluded:
        if tree_excluded_destination is not None:
            logging.info("Deleting destination excluded tree")
            fs_destination.remove_tree(path_destination, tree_excluded_destination, dry_run = args.dry_run)
        else:
            logging.info("Empty destination excluded tree")
        logging.info("")
    elif args.delete:
        if tree_unaccounted_destination_non_excluded is not None:
            logging.info("Deleting non-excluded-supporting destination unaccounted tree")
            fs_destination.remove_tree(path_destination, tree_unaccounted_destination_non_excluded, dry_run = args.dry_run)
        else:
            logging.info("Empty non-excluded-supporting destination unaccounted tree")
        logging.info("")

    files_copied = 0
    bytes_copied = 0
    files_planned, bytes_planned = _tree_file_stats(tree_copy)

    if tree_copy is not None:
        logging.info("Copying copy tree")
        if not args.dry_run and isinstance(tree_copy, dict) and _default_transfer_options(args):
            files_copied, bytes_copied = fs_destination.push_tree_here_batched(
                path_source,
                tree_copy,
                path_destination,
                fs_source,
                show_progress = args.show_progress
            )
        else:
            files_copied, bytes_copied = fs_destination.push_tree_here(
                path_source,
                fs_destination.split(path_source)[1] if isinstance(tree_copy, tuple) else ".",
                tree_copy,
                path_destination,
                fs_source,
                dry_run = args.dry_run,
                show_progress = args.show_progress
            )
    else:
        logging.info("Empty copy tree")
    logging.info("")

    files_skipped = max(total_source_files - files_planned, 0)
    elapsed = time.perf_counter() - started
    if args.dry_run:
        logging.info(f"Dry run summary: would copy {files_planned} files ({bytes_planned} bytes); "
                     f"{files_skipped} skipped; elapsed {elapsed:.2f}s")
    else:
        logging.info(f"Summary: copied {files_copied} files ({bytes_copied} bytes); "
                     f"{files_skipped} skipped; 0 failed; elapsed {elapsed:.2f}s")

if __name__ == "__main__":
    main()
