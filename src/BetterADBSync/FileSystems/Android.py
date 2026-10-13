import errno
from typing import Dict, Iterable, Iterator, List, NoReturn, Optional, Tuple
import logging
import os
import re
import stat
import shlex
import datetime
import subprocess

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

from .date_utils import datetime_string_to_posix_timestamp
from ..SAOLogging import logging_fatal

from .Base import FileSystem, chunk_arguments


class AndroidFileSystem(FileSystem):
    RE_TESTCONNECTION_NO_DEVICE = re.compile("^adb\\: no devices/emulators found$")
    RE_TESTCONNECTION_DAEMON_NOT_RUNNING = re.compile("^\\* daemon not running; starting now at tcp:\\d+$")
    RE_TESTCONNECTION_DAEMON_STARTED = re.compile("^\\* daemon started successfully$")

    RE_LS_TO_STAT = re.compile(
        r"""^
        (?:
        (?P<S_IFREG> -) |
        (?P<S_IFBLK> b) |
        (?P<S_IFCHR> c) |
        (?P<S_IFDIR> d) |
        (?P<S_IFLNK> l) |
        (?P<S_IFIFO> p) |
        (?P<S_IFSOCK> s))
        [-r][-w][-xsS]
        [-r][-w][-xsS]
        [-r][-w][-xtT] # Mode string
        [ ]+
        (?:
        [0-9]+ # Number of hard links
        [ ]+
        )?
        [^ ]+ # User name/ID
        [ ]+
        [^ ]+ # Group name/ID
        [ ]+
        (?(S_IFBLK) [^ ]+[ ]+[^ ]+[ ]+) # Device numbers
        (?(S_IFCHR) [^ ]+[ ]+[^ ]+[ ]+) # Device numbers
        (?(S_IFDIR) (?P<dirsize>[0-9]+ [ ]+))? # Directory size
        (?(S_IFREG) (?P<st_size> [0-9]+) [ ]+) # Size
        (?(S_IFLNK) ([0-9]+) [ ]+) # Link length
        (?P<st_mtime>
        [0-9]{4}-[0-9]{2}-[0-9]{2} # Date
        [ ]
        [0-9]{2}:[0-9]{2}) # Time
        [ ]
        # Don't capture filename for symlinks (ambiguous).
        (?(S_IFLNK) .* | (?P<filename> .*))
        $""", re.DOTALL | re.VERBOSE)

    RE_NO_SUCH_FILE = re.compile("^.*: No such file or directory$")
    RE_LS_NOT_A_DIRECTORY = re.compile("ls: .*: Not a directory$")
    RE_PERMISSION_DENIED = re.compile("ls: (.*): Permission denied")
    RE_TOTAL = re.compile("^total \\d+$")

    RE_REALPATH_NO_SUCH_FILE = re.compile("^realpath: .*: No such file or directory$")
    RE_REALPATH_NOT_A_DIRECTORY = re.compile("^realpath: .*: Not a directory$")

    ADBSYNC_END_OF_COMMAND = "ADBSYNC END OF COMMAND"

    def __init__(self, adb_arguments: List[str], adb_encoding: str, skip_permission_denied: bool) -> None:
        super().__init__(adb_arguments, skip_permission_denied)
        self.adb_encoding = adb_encoding
        # Device timezone for timezone-correct ls parsing / touch
        # formatting, probed once per run by _ensure_timezone(). _tzinfo None
        # means "no usable IANA zone": fall back to the constant
        # _utc_offset_seconds from `date +%z` (0 = UTC/unknown, preserving
        # the long-standing UTC behavior).
        self._tzinfo: Optional[datetime.tzinfo] = None
        self._utc_offset_seconds = 0
        self._timezone_probed = False
        self.proc_adb_shell = subprocess.Popen(
            self.adb_arguments + ["shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )

    def __del__(self):
        self.proc_adb_shell.stdin.close()
        self.proc_adb_shell.wait()

    def adb_shell(self, commands: List[str]) -> Iterator[str]:
        self.proc_adb_shell.stdin.write(shlex.join(commands).encode(self.adb_encoding))
        self.proc_adb_shell.stdin.write(" </dev/null\n".encode(self.adb_encoding))
        self.proc_adb_shell.stdin.write(shlex.join(["echo", self.ADBSYNC_END_OF_COMMAND]).encode(self.adb_encoding))
        self.proc_adb_shell.stdin.write(" </dev/null\n".encode(self.adb_encoding))
        self.proc_adb_shell.stdin.flush()

        lines_to_yield: List[str] = []
        while adb_line := self.proc_adb_shell.stdout.readline():
            try:
                adb_line = adb_line.decode(self.adb_encoding).rstrip("\r\n")
            except UnicodeDecodeError as e:
                errmsg = "Line to decode without the problematic characters: {}".format(
                    adb_line.decode(self.adb_encoding, errors='ignore'))
                e.args += (errmsg,)
                raise e
            if adb_line == self.ADBSYNC_END_OF_COMMAND:
                break
            else:
                lines_to_yield.append(adb_line)
        for line in lines_to_yield:
            yield line

    def line_not_captured(self, line: str) -> NoReturn:
        logging.critical("ADB line not captured")
        logging_fatal(line)

    def _ensure_timezone(self) -> None:
        """Probe the device timezone once per run and cache it.

        The device `ls` prints mtimes as device-local wall clock while tree
        comparisons need true epochs, and the offset depends on the DST rules
        in effect at each file's own epoch (measured on the reference
        device: a November file renders at +0100, a June file at +0200 in
        Europe/Rome). Probing the IANA zone name via `getprop` lets the
        host resolve every timestamp with its own tzdata (see
        _wall_clock_to_stat); `touch` needs the zone's *standard* offset
        because real toybox parses its time string as local standard time
        (measured: stored = timegm(string) - std_offset in all seasons).

        Fallback chain when no usable zone is reported (unusual ROMs, no
        host tzdata): the constant offset from `date +%z` (previous
        behavior), else 0 (UTC, the historical behavior). Both probes ride
        the persistent shell: no additional adb processes per run.

        The probe must not run while another adb_shell iteration is in
        progress on the shared shell process, so every caller invokes it
        before starting its own iteration (this method completes the whole
        command, including reading until the sentinel, before returning).
        """
        if self._timezone_probed:
            return
        tzinfo = None
        if ZoneInfo is not None:
            for line in self.adb_shell(["getprop", "persist.sys.timezone"]):
                name = line.strip()
                if name:
                    try:
                        tzinfo = ZoneInfo(name)
                    except (KeyError, ValueError):
                        logging.info(f"Android device timezone {name!r} not usable host-side;"
                                     " falling back to date +%z")
                        tzinfo = None
                    break
        offset = 0
        if tzinfo is None:
            for line in self.adb_shell(["date", "+%z"]):
                match = re.fullmatch(r"([+-])(\d{2})(\d{2})", line.strip())
                if match:
                    sign = 1 if match.group(1) == "+" else -1
                    offset = sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)
                    break
        else:
            logging.info(f"Android device timezone: {tzinfo.key}")
        self._tzinfo = tzinfo
        self._utc_offset_seconds = offset
        self._timezone_probed = True

    def _wall_clock_to_epoch(self, wall: str) -> int:
        """Convert an `ls` wall-clock stamp ("YYYY-MM-DD HH:MM") to an epoch.

        The stamp has minute resolution. In constant-offset mode the probed
        offset is subtracted (previous behavior). In zone mode the offset in
        effect at the file's own epoch is resolved via tzdata: candidate
        epochs are built from the offsets around the stamp (±2 days covers
        any DST transition), candidates that do not round-trip are dropped,
        and the latest surviving candidate wins. Preferring the later epoch
        on the ambiguous fall-back hour means an mtime-only change inside
        the repeated hour is at worst missed (size changes are still
        caught); the other choice would re-copy the file on every run.
        A stamp in a spring-forward gap (no valid candidate) falls back to
        the offset in effect at the stamp itself; real `ls` output can never
        hit that branch since the device only renders existing instants.
        """
        t0 = int(datetime_string_to_posix_timestamp(wall))
        zone = self._tzinfo
        if zone is None:
            return t0 - self._utc_offset_seconds

        def offset_at(epoch: int) -> int:
            # NOTE: convert via astimezone first. Calling
            # zone.utcoffset() directly on a datetime whose tzinfo is
            # datetime.timezone.utc reinterprets the UTC wall fields as
            # zone-local fields on some interpreters (observed with the
            # interpreter under test: +7200 instead of +3600 just after a
            # fall-back transition); astimezone is the documented
            # instant-preserving conversion and is correct everywhere.
            moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
            return int(moment.astimezone(zone).utcoffset().total_seconds())

        candidates = {t0 - offset_at(probe) for probe in (t0 - 172800, t0, t0 + 172800)}
        valid = [c for c in candidates if t0 - c == offset_at(c)]
        if valid:
            return max(valid)
        return t0 - offset_at(t0)

    def _touch_std_offset(self) -> int:
        """Standard offset of the device zone (what `touch -t` parses with)."""
        zone = self._tzinfo
        probe = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc).astimezone(zone)
        off = probe.utcoffset()
        dst = probe.dst() or datetime.timedelta(0)
        return int((off - dst).total_seconds())

    def test_connection(self):
        for line in self.adb_shell([":"]):
            print(line)

            if self.RE_TESTCONNECTION_DAEMON_NOT_RUNNING.fullmatch(
                    line) or self.RE_TESTCONNECTION_DAEMON_STARTED.fullmatch(line):
                continue

            raise BrokenPipeError
        self._ensure_timezone()

    def ls_to_stat(self, line: str) -> Tuple[str | None, os.stat_result]:
        if self.RE_NO_SUCH_FILE.fullmatch(line):
            raise FileNotFoundError(f'On Android: "{line}"')

        if self.RE_LS_NOT_A_DIRECTORY.fullmatch(line):
            raise NotADirectoryError(f'On Android: "{line}"')

        permission_denied = self.RE_PERMISSION_DENIED.fullmatch(line)
        if permission_denied:
            raise PermissionError(errno.EACCES, f'On Android: "{line}"', permission_denied.group(1))

        if match := self.RE_LS_TO_STAT.fullmatch(line):
            match_groupdict = match.groupdict()
            st_mode = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH  # 755
            if match_groupdict['S_IFREG']:
                st_mode |= stat.S_IFREG
            if match_groupdict['S_IFBLK']:
                st_mode |= stat.S_IFBLK
            if match_groupdict['S_IFCHR']:
                st_mode |= stat.S_IFCHR
            if match_groupdict['S_IFDIR']:
                st_mode |= stat.S_IFDIR
            if match_groupdict['S_IFIFO']:
                st_mode |= stat.S_IFIFO
            if match_groupdict['S_IFLNK']:
                st_mode |= stat.S_IFLNK
            if match_groupdict['S_IFSOCK']:
                st_mode |= stat.S_IFSOCK
            st_size = None if match_groupdict["st_size"] is None else int(match_groupdict["st_size"])
            # ls shows device-local wall clock; resolve it to the true epoch
            # with the probed timezone (per-epoch DST in zone mode, constant
            # offset otherwise, plain UTC parse when unprobed).
            st_mtime = self._wall_clock_to_epoch(match_groupdict["st_mtime"])

            # Fill the rest with dummy values.
            st_ino = 1
            st_rdev = 0
            st_nlink = 1
            st_uid = -2  # Nobody.
            st_gid = -2  # Nobody.
            st_atime = st_ctime = st_mtime

            filename = match_groupdict["filename"]
            # filename can be None on soft links. Example line:
            # 'lrw-r--r-- 1 root root 21 2022-01-01 01:00 /sdcard -> /storage/self/primary'
            if filename is not None:
                # ls -b escapes spaces - revert that
                filename = filename.replace('\\ ', ' ')
            return filename, os.stat_result(
                (st_mode, st_ino, st_rdev, st_nlink, st_uid, st_gid, st_size, st_atime, st_mtime, st_ctime))
        else:
            self.line_not_captured(line)

    @property
    def sep(self) -> str:
        return "/"

    def unlink(self, path: str) -> None:
        for line in self.adb_shell(["rm", path]):
            self.line_not_captured(line)

    def rmdir(self, path: str) -> None:
        for line in self.adb_shell(["rm", "-r", path]):
            self.line_not_captured(line)

    def makedirs(self, path: str) -> None:
        for line in self.adb_shell(["mkdir", "-p", path]):
            self.line_not_captured(line)

    def makedirs_many(self, paths: List[str]) -> None:
        overhead = self._command_overhead_bytes(["mkdir", "-p"])
        for chunk in chunk_arguments(paths, overhead_bytes = overhead):
            for line in self.adb_shell(["mkdir", "-p"] + chunk):
                self.line_not_captured(line)

    def realpath(self, path: str) -> str:
        for line in self.adb_shell(["realpath", path]):
            if self.RE_REALPATH_NO_SUCH_FILE.fullmatch(line):
                raise FileNotFoundError(f'On Android: "{line}"')
            elif self.RE_REALPATH_NOT_A_DIRECTORY.fullmatch(line):
                raise NotADirectoryError(f'On Android: "{line}"')
            else:
                return line
            # permission error possible?
        raise RuntimeError('Android: "realpath" no result')

    def lstat(self, path: str) -> os.stat_result:
        self._ensure_timezone()
        for line in self.adb_shell(["ls", "-ladb", path]):
            return self.ls_to_stat(line)[1]
        raise RuntimeError('Android: "ls -ladb" no result')

    def lstat_in_dir(self, path: str) -> Iterable[Tuple[str, os.stat_result]]:
        self._ensure_timezone()
        for line in self.adb_shell(["ls", "-lab", path]):
            if self.RE_TOTAL.fullmatch(line):
                continue
            else:
                yield self.ls_to_stat(line)

    def _format_touch_times(self, times: Tuple[int, int]) -> Tuple[str, str]:
        base = datetime.datetime(1970, 1, 1)
        atime, mtime = times
        if self._tzinfo is not None:
            # Device `touch` parses the string as local *standard* time
            # (measured: stored = timegm(string) - std_offset in all
            # seasons), so shift by the standard offset to store the true
            # epoch. Sub-minute seconds are dropped by the "%Y%m%d%H%M"
            # format (floor), matching the minute-truncated tree times.
            shift = self._touch_std_offset()
            atime += shift
            mtime += shift
        atime_str = (base + datetime.timedelta(seconds=atime)).strftime("%Y%m%d%H%M")
        mtime_str = (base + datetime.timedelta(seconds=mtime)).strftime("%Y%m%d%H%M")
        return atime_str, mtime_str

    def utime(self, path: str, times: Tuple[int, int]) -> None:
        atime, mtime = self._format_touch_times(times)
        for line in self.adb_shell(["touch", "-at", atime, "-mt", mtime, path]):
            self.line_not_captured(line)

    def utime_many(self, entries: List[Tuple[str, Tuple[int, int]]]) -> None:
        # Group by identical timestamps so one touch can stamp many paths.
        paths_by_times: Dict[Tuple[int, int], List[str]] = {}
        for path, times in entries:
            paths_by_times.setdefault(times, []).append(path)
        for times, paths in paths_by_times.items():
            atime, mtime = self._format_touch_times(times)
            fixed = ["touch", "-at", atime, "-mt", mtime]
            overhead = self._command_overhead_bytes(fixed)
            for chunk in chunk_arguments(paths, overhead_bytes = overhead):
                for line in self.adb_shell(fixed + chunk):
                    self.line_not_captured(line)

    def join(self, base: str, leaf: str) -> str:
        if base is None:
            raise ValueError("base path is None")
        if leaf is None:
            raise ValueError("leaf path is None")
        return os.path.join(base, leaf).replace("\\", "/")  # for Windows

    def split(self, path: str) -> Tuple[str, str]:
        head, tail = os.path.split(path)
        return head.replace("\\", "/"), tail  # for Windows

    def normpath(self, path: str) -> str:
        return os.path.normpath(path).replace("\\", "/")

    def push_file_here(self, source: str, destination: str, show_progress: bool = False) -> None:
        self._run_transfer_command(
            self.adb_arguments + ["push", source, destination],
            "push",
            f"{source} -> {destination}",
            show_progress = show_progress
        )

    def push_files_here(self, sources: List[str], destination_dir: str, show_progress: bool = False) -> None:
        overhead = self._command_overhead_bytes(["push", destination_dir])
        for chunk in chunk_arguments(sources, overhead_bytes = overhead):
            self._run_transfer_command(
                self.adb_arguments + ["push"] + chunk + [destination_dir],
                "push",
                f"{len(chunk)} file(s) into {destination_dir} (first: {chunk[0]})",
                show_progress = show_progress
            )

    def push_recursive_here(self, source: str, destination: str, show_progress: bool = False) -> None:
        # adb push preserves source modification times natively, matching the
        # timestamp policy of the classic path without any post-copy touch.
        self._run_transfer_command(
            self.adb_arguments + ["push", source, destination],
            "push",
            f"recursive copy of {source} -> {destination}",
            show_progress = show_progress
        )
