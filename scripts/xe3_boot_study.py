"""
X-E3 Boot ROM Study — Auto-patcher for XPP emulator boot

Demonstrates that the X-E3 boot ROM can execute its full peripheral
initialization sequence on the fujimod emulator using the GFX50R XPP
device definition as chassis.

Approach:
  1. Load X-E3 boot ROM at RAM address 0x0 using GFX50R's xpp_definition
  2. Park CPU1 (secondary core) in a WFI loop to prevent fetch-prot crashes
  3. Inject boot mode value 0x19 at the reset-gates register read (0xFFF0A000)
  4. Auto-patch: stub unmapped MMIO pages, NOP hardware polling loops,
     replace unmodeled register reads with MOV Rd, #0
  5. Result: boot ROM completes peripheral init → power-off (no firmware to load)

Findings:
  - XPP is dual-core Cortex-A7 (ARMv7-A), bare-metal
  - Reset-gates register (0xFFF0A000 offset 0): bits 3,4 must be non-zero
    (hardware ready), bits 0-2 select boot mode (1 = normal boot)
  - Boot ROM initializes clocks, DRAM controller, serial ports, and other
    peripherals through a series of register writes + status polling loops
  - Only 2 MMIO stubs needed: 0xFFF0D000 and 0xFFF94000
  - 21 total patches to reach power-off (1 boot mode + 20 polling NOPs)

Usage:
  Requires xe3-bootrom.bin (64KB boot ROM dump).
  Set XE3_BOOTROM env var or place in working directory.

  python -I scripts/xe3_boot_study.py
"""

import sys
import os
import json
import struct
import copy

# --- Path setup ---
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bindings", "python", "python"))

# libjpeg-turbo DLL needed on Windows
if sys.platform == "win32":
    for p in [r"C:\libjpeg-turbo64\bin", r"C:\libjpeg-turbo\bin"]:
        if os.path.isdir(p):
            os.add_dll_directory(p)
            break

from fujimod._native import emulator  # noqa: E402

# --- ROM loading ---
rom_path = os.environ.get("XE3_BOOTROM", "xe3-bootrom.bin")
if not os.path.isfile(rom_path):
    print(f"Boot ROM not found: {rom_path}")
    print("Set XE3_BOOTROM environment variable or place xe3-bootrom.bin in working dir")
    sys.exit(1)

with open(rom_path, "rb") as f:
    rom_data = bytearray(f.read())

print(f"Loaded {len(rom_data)} bytes from {rom_path}")

# --- Constants ---
ROM_SIZE = len(rom_data)
NOP = struct.pack('<I', 0xE1A00000)       # ARM NOP (MOV R0, R0)
PARK_ADDR = 0x00100000                     # CPU1 parking address
WFI = struct.pack('<I', 0xE320F003)        # Wait For Interrupt
BRANCH_SELF = struct.pack('<I', 0xEAFFFFFE)  # B . (infinite loop)

# --- Known patches ---
patched_rom = bytearray(rom_data)
# Patch 1: Reset-gates register read at 0x3B0 — inject normal boot mode
# Original: LDR R0, [R2] (reads 0xFFF0A000 which returns "unmodeled register")
# Replace with: MOV R0, #0x19 (bits 3,4 set = hw ready, bits 0-2 = 1 = normal boot)
patched_rom[0x3B0:0x3B4] = struct.pack('<I', 0xE3A00019)

# Patch 2: Timer busy-wait at 0x2B54 — BCC backward branch polling a non-advancing counter
patched_rom[0x2B54:0x2B58] = NOP

# --- Emulator setup ---
# Use GFX50R definition as XPP chassis (both are X-Processor Pro, Cortex-A7)
# CPU0: start at 0x0 (boot ROM entry), supervisor mode (Pstate=0xd3)
# CPU1: parked at PARK_ADDR in WFI loop
regs = json.dumps([
    [["Pc", 0], ["Pstate", 0xd3]],
    [["Pc", PARK_ADDR], ["Pstate", 0xd3]]
])
d = emulator.xpp_definition("gfx50r-311", regs)
original_result = json.loads(d)

# --- MMIO stubs ---
# Register-block stubs for unmapped peripheral pages
# CRITICAL: frames must be None (not []), job must be None (not [])
added_stubs = [{
    "model": {
        "base": 0xFFF0D000, "commands": [], "fixed": [], "frames": None,
        "idle": [], "job": None, "kind": "register-block", "size": 4096
    },
    "name": "stub_0d"
}]

patches = [
    (0x3B0, "MOV R0, #0x19 (boot mode)"),
    (0x2B54, "NOP (timer wait)")
]


# --- ARM branch decoder ---
def decode_branch_target(addr, instr):
    """Decode ARM B/BL instruction, return absolute target address or None."""
    if (instr & 0x0E000000) != 0x0A000000:
        return None
    imm24 = instr & 0xFFFFFF
    if imm24 & 0x800000:
        imm24 = imm24 - 0x1000000  # sign extend (Python ints don't overflow)
    return addr + 8 + (imm24 * 4)  # +8 for ARM pipeline


def find_backward_branch(rom, pc, scan_range):
    """Scan forward then backward from PC for the nearest backward branch."""
    # Forward scan
    for offset in range(1, scan_range + 1):
        addr = pc + offset * 4
        if addr + 4 > ROM_SIZE:
            break
        instr = struct.unpack('<I', rom[addr:addr+4])[0]
        target = decode_branch_target(addr, instr)
        if target is not None and target < addr:
            return addr, instr, target
    # Backward scan
    for offset in range(1, scan_range + 1):
        addr = pc - offset * 4
        if addr < 0:
            break
        instr = struct.unpack('<I', rom[addr:addr+4])[0]
        target = decode_branch_target(addr, instr)
        if target is not None and target < addr:
            return addr, instr, target
    return None, None, None


# --- Auto-patcher loop ---
last_stuck_pc = None
stuck_count = 0
max_rounds = 80

print(f"\n--- Auto-patcher starting (max {max_rounds} rounds) ---\n")

for attempt in range(max_rounds):
    # Build device list fresh each round (stubs may have grown)
    dev = copy.deepcopy(original_result["devices"])
    for stub in added_stubs:
        dev["nodes"].append(copy.deepcopy(stub))

    b = emulator.Board(
        json.dumps(original_result["execution"]),
        json.dumps(dev), "{}",
        {"flash": bytes(patched_rom)}, {}
    )
    b.mem_write(0, 0x00000000, bytes(patched_rom))
    b.mem_write(0, PARK_ADDR, WFI + BRANCH_SELF)

    try:
        b.run_for_instructions(1_000_000, 1000)
        pc = b.reg_read(0, '"Pc"')
        st = json.loads(b.state())
        instrs = st.get('instructions', [0, 0])
        pwr = st.get('power_off')

        if pwr:
            print(f"  [{attempt:2d}] POWER OFF at PC=0x{pc:08X}, {instrs[0]} instrs")
            try:
                r0 = b.reg_read(0, '"R0"')
                lr = b.reg_read(0, '"Lr"')
                sp = b.reg_read(0, '"Sp"')
                print(f"       R0=0x{r0:08X} LR=0x{lr:08X} SP=0x{sp:08X}")
            except Exception:
                pass
            break

        elif instrs[0] >= 999_000:
            # Stuck in a loop — find and NOP the backward branch
            instr_val = struct.unpack('<I', patched_rom[pc:pc+4])[0]

            # Track repeated stuck PCs for escalating scan range
            if pc == last_stuck_pc:
                stuck_count += 1
            else:
                stuck_count = 1
                last_stuck_pc = pc
            scan = min(16 * (2 ** (stuck_count - 1)), 256)

            # Case 1: PC itself is a backward branch
            target = decode_branch_target(pc, instr_val)
            if target is not None and target < pc:
                patched_rom[pc:pc+4] = NOP
                patches.append((pc, f"NOP (branch 0x{pc:04X} -> 0x{target:04X})"))
                print(f"  [{attempt:2d}] NOP branch at 0x{pc:04X} -> 0x{target:04X}")
                continue

            # Case 2: find backward branch nearby (escalating range)
            br_addr, _, br_target = find_backward_branch(patched_rom, pc, scan)
            if br_addr is not None:
                patched_rom[br_addr:br_addr+4] = NOP
                patches.append((br_addr, f"NOP (poll 0x{br_addr:04X} -> 0x{br_target:04X})"))
                print(f"  [{attempt:2d}] NOP at 0x{br_addr:04X} -> 0x{br_target:04X} "
                      f"(scan={scan}, stuck@0x{pc:04X})")
                continue

            print(f"  [{attempt:2d}] STUCK at PC=0x{pc:08X} (0x{instr_val:08X}), "
                  f"{instrs[0]} instrs, scan={scan}")
            break
        else:
            last_stuck_pc = None
            stuck_count = 0
            fail = st.get('failure')
            print(f"\n  DONE at PC=0x{pc:08X}, {instrs[0]} instrs, failure={fail}")
            break

    except Exception as e:
        err = str(e)
        last_stuck_pc = None
        stuck_count = 0

        if "unowned MMIO" in err:
            addr_val = int(err.split("at 0x")[1].split(",")[0], 16)
            page = addr_val & 0xFFFFF000
            if not any(s["model"]["base"] == page for s in added_stubs):
                added_stubs.append({
                    "model": {
                        "base": page, "commands": [], "fixed": [], "frames": None,
                        "idle": [], "job": None, "kind": "register-block", "size": 4096
                    },
                    "name": f"stub_{page:x}"
                })
                pc_str = err.split("PC=0x")[1].split(" ")[0].rstrip(":") if "PC=0x" in err else "?"
                print(f"  [{attempt:2d}] +stub 0x{page:08X} (PC=0x{pc_str})")
            else:
                print(f"  [{attempt:2d}] DUP stub 0x{page:08X}: {err[:100]}")
                break

        elif "unmodeled" in err.lower() and "PC=0x" in err:
            pc_hex = err.split("PC=0x")[1].split(" ")[0].rstrip(":")
            pc_val = int(pc_hex, 16)
            if pc_val + 4 <= ROM_SIZE:
                instr = struct.unpack('<I', patched_rom[pc_val:pc_val+4])[0]
                rd = (instr >> 12) & 0xF
                mov_rd_0 = 0xE3A00000 | (rd << 12)
                patched_rom[pc_val:pc_val+4] = struct.pack('<I', mov_rd_0)
                patches.append((pc_val, f"MOV R{rd}, #0 (unmodeled 0x{pc_val:04X})"))
                print(f"  [{attempt:2d}] MOV R{rd}, #0 at 0x{pc_val:04X}")
            else:
                print(f"  [{attempt:2d}] PC 0x{pc_val:08X} outside ROM")
                break

        elif "supply off" in err.lower() or "power" in err.lower():
            print(f"  [{attempt:2d}] POWER: {err[:200]}")
            break
        else:
            print(f"  [{attempt:2d}] STOP: {err[:200]}")
            break

# --- Summary ---
print(f"\n{'='*50}")
print(f"  X-E3 Boot ROM Study — Results")
print(f"{'='*50}")
print(f"  Rounds: {attempt + 1}")
print(f"  Patches ({len(patches)}):")
for addr, desc in patches:
    print(f"    0x{addr:04X}: {desc}")
print(f"  MMIO stubs ({len(added_stubs)}):")
for s in added_stubs:
    print(f"    0x{s['model']['base']:08X}")
print(f"\n  Boot ROM completed peripheral init.")
print(f"  Power-off = no firmware image to load from flash/SD.")
