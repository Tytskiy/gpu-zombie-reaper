****# GPU Zombie Reaper

A simple tool to kill GPU processes based on various criteria.

## The Problem

Oh cool, all your VRAM is taken! Except nothing is actually running. It's just dead processes squatting on gigabytes of memory doing absolutely NOTHING. 

<img src="zombies_around.png" alt="GPU Zombies Being Annoying" width="600">

*0% GPU utilization. 100% memory hogging. Maximum audacity.*

This tool kills them. You're welcome. 💀

## Features

- Kill processes with zero GPU utilization
- Kill zombie processes
- Kill processes older than specified hours
- Dry-run mode to preview actions
- Works with or without sudo

## Installation

### Option 1: Install from source (recommended)

```bash
git clone https://github.com/tytskiy/gpu-zombie-reaper.git
cd gpu-zombie-reaper
pip install -e .
```

```

### Option 2: Clone and install dependencies

```bash
git clone https://github.com/tytskiy/gpu-zombie-reaper.git
cd gpu-zombie-reaper
pip install -r requirements.txt
```

## Usage

### Running as a Python module (recommended)

**With sudo (direct kill):**
```bash
sudo python3 -m gpu_zombie_reaper --zero-util
```

**Without sudo (get PIDs to kill manually):**
```bash
    python -m gpu_zombie_reaper --zero-util --fuser-output "$(sudo fuser -v /dev/nvidia* 2>/dev/null)" --output-pids \
    | xargs sudo kill -9
```

### Running the script directly

**With sudo (direct kill):**
```bash
sudo python3 gpu_zombie_reaper.py --zero-util
```

**Without sudo (get PIDs to kill manually):**
```bash
sudo fuser -v /dev/nvidia* 2>/dev/null \
    | python3 gpu_zombie_reaper.py --zero-util --fuser-output /dev/stdin --output-pids \
    | xargs sudo kill -9
```

## Options

- `--dry-run` - Preview what would be killed
- `--zero-util` - Kill processes with 0% GPU utilization
- `--zombies` - Kill zombie processes
- `--too-old HOURS` - Kill processes running longer than specified hours
- `--no-process` - Kill processes without system info
- `--output-pids` - Output PIDs only (for no-sudo mode)

## Examples

```bash
# Preview what would be killed
sudo python3 -m gpu_zombie_reaper --zero-util --dry-run

# Kill old processes (older than 24 hours)
sudo python3 -m gpu_zombie_reaper --too-old 24

# Kill zombie processes
sudo python3 -m gpu_zombie_reaper --zombies

# Combine multiple criteria
sudo python3 -m gpu_zombie_reaper --zero-util --zombies --too-old 12

# Or run the script directly:
sudo python3 gpu_zombie_reaper.py --zero-util --dry-run
```

## License  

This projects uses the [WTFPL license](http://www.wtfpl.net/)
(Do **W**hat **T**he **F**uck You Want To **P**ublic **L**icense)
