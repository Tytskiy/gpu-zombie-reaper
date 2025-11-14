#!/usr/bin/env python3
"""
GPU Process Management Tool

Kill GPU processes based on various criteria:
- Zero GPU utilization
- Zombie processes
- Processes older than specified hours
- Processes without system info

Modes of operation:
1. Simple mode (requires sudo): Direct kill with sudo privileges
2. No-sudo mode: Provide fuser output and get PIDs to kill manually

Simple mode example:
    sudo python3 gpu_zombie_reaper.py --zero-util

No-sudo mode example:
    sudo fuser -v /dev/nvidia* 2>/dev/null \
        | python3 gpu_zombie_reaper.py --zero-util --fuser-output /dev/stdin --output-pids \
        | xargs sudo kill -9
"""

import argparse
import dataclasses
import os
import signal
import subprocess
import time
from typing import Callable, Optional

import psutil
import pynvml


WHITELISTED_PROCESS_NAMES = {
    "nv-fabricmanager",
    "nvitop",
    "nvtop",
    "nvidia-persistenced",
    "nvidia-smi",
}


class Colors:
    """ANSI color codes for pretty printing."""

    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"


@dataclasses.dataclass
class GpuInfo:
    """Information about GPU usage."""

    idx: int
    util: float  # [0, 100] - NOTE: This is device-level, not per-process
    mem: float  # MB


@dataclasses.dataclass
class ProcessInfo:
    """Combined information about a process from GPU and system perspective."""

    pid: int
    gpu_info: Optional[GpuInfo] = None
    sys_info: Optional[psutil.Process] = None

    @property
    def has_gpu_info(self) -> bool:
        return self.gpu_info is not None

    @property
    def has_sys_info(self) -> bool:
        return self.sys_info is not None

    @property
    def is_zombie(self) -> bool:
        return self.sys_info is not None and self.sys_info.status() == psutil.STATUS_ZOMBIE

    @property
    def execution_time(self) -> Optional[float]:
        """Returns execution time in seconds."""
        if self.sys_info is not None:
            try:
                return time.time() - self.sys_info.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
        return None

    @property
    def cmdline(self) -> Optional[str]:
        if self.sys_info is not None:
            try:
                return " ".join(self.sys_info.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                return None
        return None

    @property
    def name(self) -> Optional[str]:
        if self.sys_info is not None:
            try:
                return self.sys_info.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
        return None

    @property
    def username(self) -> Optional[str]:
        if self.sys_info is not None:
            try:
                return self.sys_info.username()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
        return None

    def __repr__(self) -> str:
        parts = []

        parts.append(f"{Colors.BOLD}PID {self.pid}{Colors.ENDC}")

        if self.username:
            parts.append(f"{Colors.OKCYAN}{self.username}{Colors.ENDC}")
        if self.name:
            parts.append(f"{Colors.OKBLUE}[{self.name}]{Colors.ENDC}")

        if self.gpu_info is not None:
            gpu_str = f"{Colors.WARNING}GPU {self.gpu_info.idx}{Colors.ENDC}"
            mem_str = f"{self.gpu_info.mem:>7.0f}MB"

            util = self.gpu_info.util
            if util == 0:
                util_str = f"{Colors.DIM}{util:>3.0f}%{Colors.ENDC}"
            elif util < 50:
                util_str = f"{Colors.WARNING}{util:>3.0f}%{Colors.ENDC}"
            else:
                util_str = f"{Colors.OKGREEN}{util:>3.0f}%{Colors.ENDC}"

            parts.append(f"{gpu_str} | {mem_str} | {util_str}")

        if self.execution_time is not None:
            hours = self.execution_time / 3600
            if hours < 1:
                runtime_str = f"{self.execution_time / 60:>5.1f}m"
            else:
                runtime_str = f"{hours:>5.1f}h"
            parts.append(f"⏱  {runtime_str}")

        if self.is_zombie:
            parts.append(f"{Colors.FAIL}💀 ZOMBIE{Colors.ENDC}")

        if self.cmdline:
            cmd = self.cmdline
            if len(cmd) > 100:
                cmd = cmd[:97] + "..."
            parts.append(f"\n    {Colors.DIM}└─ {cmd}{Colors.ENDC}")

        return " ".join(parts)


def get_processes_from_nvml() -> dict[int, ProcessInfo]:
    """Get GPU processes using NVIDIA Management Library."""
    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError as e:
        print(f"Failed to initialize NVML: {e}")
        return {}

    result = {}
    try:
        deviceCount = pynvml.nvmlDeviceGetCount()

        for gpu_idx in range(deviceCount):
            handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
            processes = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)

            # Get device utilization (same for all processes on this GPU)
            try:
                util: float = float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
            except pynvml.NVMLError:
                util = 0.0

            for proc in processes:
                pid = proc.pid
                mem = proc.usedGpuMemory / (1024**2)

                try:
                    info = psutil.Process(pid)
                except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied) as e:
                    info = None
                    print(f"Warning: Cannot access process {pid}: {e}")

                process_info = ProcessInfo(
                    pid=pid,
                    gpu_info=GpuInfo(
                        idx=gpu_idx,
                        util=util,
                        mem=mem,
                    ),
                    sys_info=info,
                )

                result[pid] = process_info
    finally:
        pynvml.nvmlShutdown()

    return result


def get_processes_from_dev_nvidia(fuser_output: Optional[str] = None) -> dict[int, ProcessInfo]:
    """Get processes using /dev/nvidia* devices.

    Args:
        fuser_output: Optional pre-captured output from 'fuser -v /dev/nvidia*'.
                     If None, will run the command with sudo.
    """
    if fuser_output is None:
        # Run the command with sudo (original behavior)
        subprocess_result = subprocess.run(
            "sudo fuser -v /dev/nvidia* 2>/dev/null",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = subprocess_result.stdout.strip()
    else:
        # Use provided output (no sudo mode)
        output = fuser_output.strip()

    if not output:
        return {}

    try:
        pids = [int(x) for x in output.strip().split()]
    except ValueError as e:
        print(output.split())
        print(f"Warning: Failed to parse PIDs from fuser output: {e}")
        return {}

    result = {}
    for pid in pids:
        try:
            result[pid] = ProcessInfo(pid=pid, gpu_info=None, sys_info=psutil.Process(pid))
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied) as e:
            result[pid] = ProcessInfo(pid=pid)
            print(f"Warning: Cannot access process {pid}: {e}")

    return result


# ============================================================================
# PROCESS FILTERING
# ============================================================================


def is_whitelisted(info: ProcessInfo) -> bool:
    """Check if a process should be whitelisted (not killed)."""
    name = info.name
    if name is None:
        return False
    return name in WHITELISTED_PROCESS_NAMES


# ============================================================================
# PROCESS ACTIONS
# ============================================================================


def kill_by_predicate(
    processes: list[ProcessInfo],
    predicate: Callable[[ProcessInfo], bool],
    dry_run: bool = True,
    output_pids_only: bool = False,
) -> tuple[int, list[int]]:
    """Kill processes matching the predicate.

    Args:
        processes: List of processes to check
        predicate: Function to determine if a process should be killed
        dry_run: If True, don't actually kill processes
        output_pids_only: If True, just collect PIDs without verbose output

    Returns:
        Tuple of (count, list of PIDs)
    """
    killed_count = 0
    pids_to_kill = []

    for proc in processes:
        try:
            if predicate(proc):
                pids_to_kill.append(proc.pid)

                if output_pids_only:
                    # Silent mode for PID collection
                    killed_count += 1
                elif dry_run:
                    icon = "👁️ "
                    action = f"{Colors.WARNING}[DRY RUN] Would kill{Colors.ENDC}"
                    print(f"  {icon} {action}: {proc}")
                    killed_count += 1
                else:
                    icon = "🔪"
                    action = f"{Colors.FAIL}Killing{Colors.ENDC}"
                    print(f"  {icon} {action}: {proc}")

                    try:
                        os.kill(proc.pid, signal.SIGKILL)
                        killed_count += 1
                        print(f"    {Colors.OKGREEN}✓ Successfully killed{Colors.ENDC}")
                    except ProcessLookupError:
                        print(f"    {Colors.DIM}✗ Process {proc.pid} no longer exists{Colors.ENDC}")
                    except PermissionError:
                        print(f"    {Colors.FAIL}✗ Permission denied to kill process {proc.pid}{Colors.ENDC}")
        except Exception as e:
            if not output_pids_only:
                print(f"  {Colors.FAIL}✗ ERROR: Failed to process {proc.pid}: {e}{Colors.ENDC}")

    return killed_count, pids_to_kill


# ============================================================================
# UI / DISPLAY
# ============================================================================


def print_header(title: str, emoji: str = "🔍"):
    """Print a nice section header."""
    width = 88
    print(f"\n{Colors.BOLD}{Colors.HEADER}{'═' * width}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.HEADER}{emoji}  {title}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.HEADER}{'═' * width}{Colors.ENDC}")


def print_summary(dry_run: bool, count: int):
    """Print a summary of the kill operation."""
    if count > 0:
        action = "Would kill" if dry_run else "Killed"
        color = Colors.WARNING if dry_run else Colors.OKGREEN
        icon = "👁️ " if dry_run else "✓ "
        print(f"\n{color}{Colors.BOLD}{icon}{action} {count} process(es){Colors.ENDC}\n")
    else:
        print(f"\n{Colors.DIM}No processes matched this criteria{Colors.ENDC}\n")


# ============================================================================
# ARGUMENT PARSING
# ============================================================================


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Kill GPU processes based on various criteria. Requires sudo for /dev/nvidia* checks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple mode (requires sudo):
  sudo python3 gpu_zombie_reaper.py --zero-util

  # No-sudo mode - provide fuser output and get PIDs to kill:
    python -m gpu_zombie_reaper --zero-util --fuser-output "$(sudo fuser -v /dev/nvidia* 2>/dev/null)" --output-pids \
    | xargs sudo kill -9
        """,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be killed without actually killing processes",
    )
    parser.add_argument(
        "--zero-util",
        action="store_true",
        default=False,
        help="Kill processes with zero GPU utilization",
    )
    parser.add_argument(
        "--zombies",
        action="store_true",
        default=False,
        help="Kill zombie processes",
    )
    parser.add_argument(
        "--no-process",
        action="store_true",
        default=False,
        help="Kill processes without system info",
    )
    parser.add_argument(
        "--too-old",
        type=int,
        default=None,
        metavar="HOURS",
        help="Kill processes running for more than HOURS hours",
    )
    parser.add_argument(
        "--fuser-output",
        type=str,
        default=None,
        metavar="OUTPUT",
        help="Provide output from 'sudo fuser -v /dev/nvidia* 2>/dev/null' instead of running with sudo",
    )
    parser.add_argument(
        "--output-pids",
        action="store_true",
        default=False,
        help="Output PIDs to kill (one per line) instead of actually killing them. For use without sudo.",
    )

    return parser.parse_args()


def main():
    """Main entry point for the script."""
    args = parse_arguments()

    # Check for incompatible options
    if args.output_pids and not args.dry_run:
        # In output-pids mode, we're not actually killing, so dry_run should be implicit
        args.dry_run = True

    # Gather process information
    if not args.output_pids:
        print(f"{Colors.OKCYAN}🔍 Gathering GPU process information...{Colors.ENDC}")

    nvml_processes: dict[int, ProcessInfo] = get_processes_from_nvml()
    dev_nv_processes: dict[int, ProcessInfo] = get_processes_from_dev_nvidia(fuser_output=args.fuser_output)

    # Merge processes from both sources
    all_processes: dict[int, ProcessInfo] = nvml_processes.copy()
    for pid, info in dev_nv_processes.items():
        if pid not in all_processes:
            all_processes[pid] = info

    # Filter out whitelisted processes
    all_processes = {pid: proc for pid, proc in all_processes.items() if not is_whitelisted(proc)}
    all_processes_list: list[ProcessInfo] = list(all_processes.values())

    # Display process count
    if not args.output_pids:
        if len(all_processes_list) == 0:
            print(f"{Colors.OKGREEN}✓ Found 0 GPU processes{Colors.ENDC}\n")
        else:
            print(
                f"{Colors.OKGREEN}✓ Found {len(all_processes_list)} GPU processes (after whitelist filtering){Colors.ENDC}\n"
            )

    # Execute kill operations based on criteria
    total_killed = 0
    all_pids_to_kill = []

    if args.zero_util:
        if not args.output_pids:
            print_header("ZERO GPU UTILIZATION", "⚠️")
        count, pids = kill_by_predicate(
            all_processes_list,
            lambda proc: proc.gpu_info is not None and proc.gpu_info.util == 0,
            args.dry_run,
            args.output_pids,
        )
        if not args.output_pids:
            print_summary(args.dry_run, count)
        total_killed += count
        all_pids_to_kill.extend(pids)

    if args.too_old is not None:
        if not args.output_pids:
            print_header(f"PROCESSES OLDER THAN {args.too_old} HOURS", "⏰")
        count, pids = kill_by_predicate(
            all_processes_list,
            lambda proc: (
                proc.has_sys_info and proc.execution_time is not None and proc.execution_time / 3600 > args.too_old
            ),
            args.dry_run,
            args.output_pids,
        )
        if not args.output_pids:
            print_summary(args.dry_run, count)
        total_killed += count
        all_pids_to_kill.extend(pids)

    if args.zombies:
        if not args.output_pids:
            print_header("ZOMBIE PROCESSES", "💀")
        count, pids = kill_by_predicate(
            all_processes_list,
            lambda proc: proc.is_zombie,
            args.dry_run,
            args.output_pids,
        )
        if not args.output_pids:
            print_summary(args.dry_run, count)
        total_killed += count
        all_pids_to_kill.extend(pids)

    if args.no_process:
        if not args.output_pids:
            print_header("PROCESSES WITHOUT SYSTEM INFO", "❓")
        count, pids = kill_by_predicate(
            all_processes_list,
            lambda proc: not proc.has_sys_info,
            args.dry_run,
            args.output_pids,
        )
        if not args.output_pids:
            print_summary(args.dry_run, count)
        total_killed += count
        all_pids_to_kill.extend(pids)

    # Handle output-pids mode
    if args.output_pids:
        # Output PIDs in clean format (one per line) for piping to kill command
        for pid in sorted(set(all_pids_to_kill)):  # Remove duplicates and sort
            print(pid)
        exit(0)

    # Final summary
    if total_killed == 0:
        if not any([args.zero_util, args.too_old, args.zombies, args.no_process]):
            print(f"\n{Colors.WARNING}ℹ️  No kill criteria specified. Use --help to see available options.{Colors.ENDC}")
        else:
            print(f"\n{Colors.OKGREEN}✓ No processes matched the specified criteria.{Colors.ENDC}")
    else:
        print(f"\n{Colors.BOLD}{'═' * 88}{Colors.ENDC}")
        action = "Would kill" if args.dry_run else "Killed"
        color = Colors.WARNING if args.dry_run else Colors.OKGREEN
        icon = "👁️ " if args.dry_run else "✅"
        print(f"{color}{Colors.BOLD}{icon}  TOTAL: {action} {total_killed} process(es){Colors.ENDC}")
        if args.dry_run:
            print(f"{Colors.DIM}   Run without --dry-run to actually kill the processes.{Colors.ENDC}")
        print(f"{Colors.BOLD}{'═' * 88}{Colors.ENDC}\n")


if __name__ == "__main__":
    main()
